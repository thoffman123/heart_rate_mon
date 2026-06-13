#!/usr/bin/env python3
"""
respiration.py — Derive a breathing signal from Polar H10 data.

Two independent estimators, both well established for chest-worn sensors:

  • ACC method  — the strap physically moves with the ribcage.  The three
                  accelerometer axes are detrended, band-pass filtered in the
                  respiration band, and projected onto their first principal
                  component, giving an orientation-independent breathing wave.

  • RSA method  — respiratory sinus arrhythmia: heart rate rises on inhale and
                  falls on exhale.  The instantaneous-HR tachogram (from RR
                  intervals) is resampled to a uniform grid and band-pass
                  filtered in the same band.  Always available whenever HR is
                  streaming, even without ECG or ACC.

Both paths share the same back end: resample → detrend → band-pass → estimate
rate (FFT peak with parabolic interpolation), instantaneous phase (Hilbert),
and a 0–1 quality score (spectral concentration in the breathing band).

The waveform that drives real-time feedback is produced by a phase-locked
oscillator rather than read straight off the filtered buffer edge: the FFT
gives a robust rate, the Hilbert transform (taken a little back from the noisy
edge) gives a phase reference, and an oscillator locked to them advances on
wall-clock time.  This keeps the output smooth and continuous even when the
sensor delivers samples slowly or irregularly.

The estimator is stateful: feed it samples as they arrive, then call
estimate() whenever you want the current breathing estimate.  It is signal /
transport agnostic, so the same class works from the CLI, the GUI, or a file
replay.

Run `python respiration.py` to exercise both methods on synthetic data with no
hardware attached.
"""

from __future__ import annotations

import collections
import math
import time
from typing import Optional

import numpy as np

try:
    from scipy.signal import butter, sosfiltfilt, hilbert
    _HAVE_SCIPY = True
except Exception:  # pragma: no cover - scipy is a declared dependency
    _HAVE_SCIPY = False


# ── Defaults ──────────────────────────────────────────────────────────────────
# Breathing band: 0.05 Hz (3 br/min, very slow meditative) to 0.7 Hz (42 br/min).
# Resonance / coherent breathing sits at ~0.1 Hz (6 br/min), comfortably inside.
DEFAULT_BAND      = (0.05, 0.70)
DEFAULT_WINDOW_S  = 45.0     # analysis window length
ACC_FS_RS         = 10.0     # ACC resample rate (Hz) — plenty for <1 Hz breathing
RSA_FS_RS         = 4.0      # RR tachogram resample rate (Hz) — standard for RSA
MIN_SECONDS       = 15.0     # minimum data before any estimate is produced
MIN_RATE_SECONDS  = 22.0     # minimum data before a rate is trusted
MIN_RATE_QUALITY  = 0.5      # spectral peak must be this clean before a rate is trusted
ACC_LIVE_TIMEOUT  = 3.0      # 'auto' treats ACC as live if fed within this many s

# Phase-locked oscillator (drives the smooth, real-time breathing waveform).
# The phase correction is slew-limited so the oscillator phase never runs backward
# (see estimate()): the bar advances monotonically through each breath, free of the
# jitter the raw Hilbert edge phase shows at the H10's sparse effective ACC rate.
OSC_LOCK_GAIN     = 0.08     # correction toward the measured phase, per estimate() call
OSC_FREQ_GAIN     = 0.05     # smoothing of the oscillator frequency, per call
OSC_EDGE_GUARD_S  = 1.5      # back off this many seconds from the noisy buffer edge

# Harmonic guard: a non-sinusoidal breathing waveform carries real power at 2·f0,
# 3·f0…; in a single window a harmonic can momentarily out-power the fundamental and
# the raw peak-pick jumps to ~k·f0 (e.g. 4.7→15.1 br/min). If a subharmonic f/k is in
# band and still holds at least this fraction of the peak's power, it is the true
# fundamental and we snap to it. Genuine fast breathing has no subharmonic energy, so
# the guard never pulls a real high rate down.
HARMONIC_SUBPEAK_RATIO = 0.4
HARMONIC_MAX_K         = 3   # check down to the 3rd harmonic


def _bandpass_sos(fs: float, band: tuple):
    """2nd-order Butterworth band-pass as SOS, with the high edge clamped below Nyquist."""
    lo, hi = band
    nyq = fs / 2.0
    hi = min(hi, nyq * 0.95)
    lo = max(lo, 1e-3)
    if lo >= hi:
        return None
    return butter(2, [lo, hi], btype="band", fs=fs, output="sos")


def _wrap_pi(a: float) -> float:
    """Wrap an angle (radians) to (-π, π]."""
    return (a + math.pi) % (2.0 * math.pi) - math.pi


def _dominant_frequency(sig: np.ndarray, fs: float, band: tuple) -> tuple:
    """
    Return (freq_hz, quality) for the dominant spectral peak inside `band`.

    quality is the fraction of total spectral power concentrated in a narrow
    region around the peak — a simple, interpretable 0–1 confidence measure.
    Returns (None, 0.0) if no usable peak is found.
    """
    n = len(sig)
    if n < 8:
        return None, 0.0

    # Hann window reduces spectral leakage before the FFT.
    win = np.hanning(n)
    spectrum = np.abs(np.fft.rfft(sig * win))
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    power = spectrum ** 2

    band_mask = (freqs >= band[0]) & (freqs <= band[1])
    if not np.any(band_mask):
        return None, 0.0

    band_idx = np.where(band_mask)[0]
    peak_rel = int(np.argmax(power[band_idx]))
    peak_idx = int(band_idx[peak_rel])
    if power[peak_idx] <= 0:
        return None, 0.0

    # ── Harmonic guard ──────────────────────────────────────────────────────────
    # If a subharmonic f_peak/k lands in band and still carries a strong share of the
    # peak's power, the raw peak was a harmonic — snap down to the fundamental. Check
    # the deepest harmonic first so a 3·f0 peak resolves to f0, not 1.5·f0.
    bin_w = float(freqs[1] - freqs[0]) if len(freqs) > 1 else 0.0
    chosen_idx = peak_idx
    if bin_w > 0:
        peak_power = float(power[peak_idx])
        band_lo, band_hi = int(band_idx[0]), int(band_idx[-1])
        for k in range(HARMONIC_MAX_K, 1, -1):
            f_sub = freqs[peak_idx] / k
            if f_sub < band[0]:
                continue
            j = int(round(f_sub / bin_w))
            # Keep the ±1-bin search strictly inside the band: otherwise argmax can
            # latch a sub-band drift bin and report an impossible < band[0] rate.
            lo, hi = max(j - 1, band_lo), min(j + 2, band_hi + 1)
            if lo >= hi:
                continue
            sub_idx = lo + int(np.argmax(power[lo:hi]))
            if float(power[sub_idx]) >= HARMONIC_SUBPEAK_RATIO * peak_power:
                chosen_idx = sub_idx
                break

    # Parabolic interpolation around the chosen bin for sub-bin frequency accuracy.
    # delta is clamped to ±0.5 bin to guard against catastrophic float cancellation
    # when adjacent FFT bins have nearly equal power (denom ≈ 0).
    f_peak = freqs[chosen_idx]
    if 0 < chosen_idx < len(power) - 1:
        a, b, c = power[chosen_idx - 1], power[chosen_idx], power[chosen_idx + 1]
        denom = (a - 2 * b + c)
        if denom != 0:
            delta = max(-0.5, min(0.5, 0.5 * (a - c) / denom))
            f_peak = freqs[chosen_idx] + delta * bin_w

    # Sub-bin interpolation can still nudge the band-edge bin just outside the band;
    # clamp so the reported frequency is never below/above the physical band.
    f_peak = min(max(f_peak, band[0]), band[1])

    # Quality: power within ±1 bin of the chosen peak relative to total spectral power.
    lo_b = max(0, chosen_idx - 1)
    hi_b = min(len(power), chosen_idx + 2)
    total = float(np.sum(power))
    quality = float(np.sum(power[lo_b:hi_b]) / total) if total > 0 else 0.0
    return float(f_peak), max(0.0, min(1.0, quality))


def _resample_uniform(t: np.ndarray, v: np.ndarray, fs: float) -> Optional[tuple]:
    """Linearly resample irregular samples (t, v) onto a uniform grid at fs Hz."""
    if len(t) < 4:
        return None
    t0, t1 = float(t[0]), float(t[-1])
    if t1 - t0 < 1.0:
        return None
    # np.interp requires strictly increasing xp; non-monotonic timestamps (from
    # a corrupted delta-decoder frame whose samples extend far into the future,
    # interleaving with later real frames) produce undefined garbage output.
    if not np.all(np.diff(t) > 0):
        return None
    grid = np.arange(t0, t1, 1.0 / fs)
    if len(grid) < 8:
        return None
    vi = np.interp(grid, t, v)
    return grid, vi


class RespirationEstimator:
    """Stateful breathing-signal estimator. Thread-unsafe; call from one thread."""

    def __init__(
        self,
        method: str = "auto",
        window_s: float = DEFAULT_WINDOW_S,
        band: tuple = DEFAULT_BAND,
    ) -> None:
        self.method = method if method in ("auto", "acc", "rsa") else "auto"
        self.window_s = float(window_s)
        self.band = band

        # ACC buffers — relative device-clock seconds + raw axes (milliG)
        self._acc_t: collections.deque = collections.deque()
        self._acc_x: collections.deque = collections.deque()
        self._acc_y: collections.deque = collections.deque()
        self._acc_z: collections.deque = collections.deque()
        self._acc_t0: Optional[float] = None
        self._last_acc_wall = 0.0

        # RSA buffers — cumulative beat time (s) + instantaneous HR (bpm)
        self._rr_t: collections.deque = collections.deque()
        self._rr_hr: collections.deque = collections.deque()
        self._rr_clock = 0.0          # running cumulative beat time
        self._last_rr_wall = 0.0

        # Phase-locked oscillator state — produces a smooth, continuous breathing
        # waveform from the (robust) detected rate + phase, instead of reading the
        # fragile filtered value at the buffer edge. Holds up at sparse sample rates.
        self._osc_phase: Optional[float] = None   # radians, 0 = waveform peak
        self._osc_freq:  float = 0.10              # Hz, last good breathing frequency
        self._osc_mono:  Optional[float] = None    # clock of the last estimate() call

    # ── Feed: accelerometer ────────────────────────────────────────────────────
    def add_acc_frame(self, frame_timestamp_ns: Optional[int], fs: float, samples: list) -> None:
        """Add one ACC frame. Samples are dicts {'x','y','z'} in milliG, oldest first."""
        if not samples or fs <= 0:
            return
        # Build a relative device-clock time axis from the frame timestamp.
        if frame_timestamp_ns is not None:
            frame_t = frame_timestamp_ns / 1e9
        else:
            frame_t = time.monotonic()
        if self._acc_t0 is None:
            self._acc_t0 = frame_t
        n = len(samples)
        dt = 1.0 / fs
        for i, s in enumerate(samples):
            self._acc_t.append((frame_t - self._acc_t0) + i * dt)
            self._acc_x.append(float(s.get("x", 0.0)))
            self._acc_y.append(float(s.get("y", 0.0)))
            self._acc_z.append(float(s.get("z", 0.0)))
        self._last_acc_wall = time.monotonic()
        self._trim_acc()

    def _trim_acc(self) -> None:
        if not self._acc_t:
            return
        t_last = self._acc_t[-1]
        cutoff = t_last - self.window_s
        while self._acc_t and self._acc_t[0] < cutoff:
            self._acc_t.popleft()
            self._acc_x.popleft()
            self._acc_y.popleft()
            self._acc_z.popleft()

    # ── Feed: RR intervals ─────────────────────────────────────────────────────
    def add_rr(self, rr_ms: float) -> None:
        """Add one RR (beat-to-beat) interval in milliseconds."""
        if rr_ms is None:
            return
        # Plausibility gate — reject artifacts (30–200 bpm).
        if not (300.0 <= rr_ms <= 2000.0):
            return
        self._rr_clock += rr_ms / 1000.0
        self._rr_t.append(self._rr_clock)
        self._rr_hr.append(60000.0 / rr_ms)   # instantaneous HR; peaks ≈ inhale
        self._last_rr_wall = time.monotonic()
        self._trim_rr()

    def _trim_rr(self) -> None:
        if not self._rr_t:
            return
        t_last = self._rr_t[-1]
        cutoff = t_last - self.window_s
        while self._rr_t and self._rr_t[0] < cutoff:
            self._rr_t.popleft()
            self._rr_hr.popleft()

    # ── Method selection ───────────────────────────────────────────────────────
    def _acc_is_live(self) -> bool:
        return (
            len(self._acc_t) > 0
            and (time.monotonic() - self._last_acc_wall) < ACC_LIVE_TIMEOUT
        )

    def _choose_method(self) -> str:
        if self.method == "acc":
            return "acc"
        if self.method == "rsa":
            return "rsa"
        # auto: prefer ACC for a chest strap when it is actively streaming.
        return "acc" if self._acc_is_live() else "rsa"

    # ── Estimate ───────────────────────────────────────────────────────────────
    def estimate(self, now: Optional[float] = None) -> Optional[dict]:
        """
        Compute the current breathing estimate, or None if there is not yet
        enough data. Returns a dict with method, breathing_rate_brpm, waveform
        (normalized −1..1), phase_rad (0..2π), quality (0..1), and window_s.

        `now` is the current clock in seconds (defaults to time.monotonic()); it
        drives the phase oscillator and can be injected for offline replay.
        """
        if not _HAVE_SCIPY:
            return None
        if now is None:
            now = time.monotonic()
        method = self._choose_method()
        if method == "acc":
            built = self._build_acc_signal()
            fs = ACC_FS_RS
        else:
            built = self._build_rsa_signal()
            fs = RSA_FS_RS
        if built is None:
            return None
        sig, span_s = built
        if span_s < MIN_SECONDS or len(sig) < 16:
            return None

        sos = _bandpass_sos(fs, self.band)
        if sos is None:
            return None
        try:
            filtered = sosfiltfilt(sos, sig)
        except Exception:
            return None

        freq, quality = _dominant_frequency(filtered, fs, self.band)

        # Breathing amplitude: RMS of the band-passed signal. For ACC this is the
        # depth of chest motion in milliG (clean meditative breathing reads ~20 mg and
        # scores high quality; the noise floor sits below ~10 mg); for RSA it is the
        # HR-modulation depth in bpm. Exposed so a UI can show whether the sensor is
        # picking up signal — but quality, not amplitude, is the trustworthiness metric.
        amplitude = float(np.sqrt(np.mean(filtered ** 2)))

        # ── Smooth waveform via a phase-locked oscillator ───────────────────────
        # Reading filtered[-1] directly is fragile: zero-phase filtering and the
        # Hilbert transform both have their largest artifacts at the newest sample,
        # and with a sparse sensor rate that edge is mostly interpolation — so the
        # instantaneous value collapses toward zero and twitches when a new sample
        # lands. Instead we lock an oscillator to the detected frequency and correct
        # its phase from the Hilbert phase taken a little *back* from the edge (then
        # projected forward). The oscillator advances on wall-clock time, so the
        # output stays smooth between — and faster than — incoming samples.
        f_use = freq if (freq is not None and freq > 0) else self._osc_freq

        # Measured phase: backed off from the noisy edge, then projected to the edge.
        try:
            analytic = hilbert(filtered)
            guard = int(max(1, round(OSC_EDGE_GUARD_S * fs)))
            guard = min(guard, len(analytic) - 1)
            phi_back = float(np.angle(analytic[-1 - guard]))
            phi_edge = phi_back + 2.0 * math.pi * f_use * (guard / fs)
        except Exception:
            phi_edge = self._osc_phase if self._osc_phase is not None else 0.0

        if self._osc_phase is None or self._osc_mono is None:
            # First lock — snap straight to the measured phase.
            self._osc_phase = phi_edge
            self._osc_freq = f_use
        else:
            dt = now - self._osc_mono
            dt = max(0.0, min(dt, 2.0))            # clamp gaps / clock jumps
            if freq is not None and freq > 0:
                self._osc_freq += OSC_FREQ_GAIN * (freq - self._osc_freq)
            # Free-run at the current breathing frequency …
            freerun = 2.0 * math.pi * self._osc_freq * dt
            # … then pull toward the measured phase, weighted by signal quality
            # (low quality → the oscillator coasts instead of chasing noise).  The
            # error is measured against the *post-free-run* phase, and the correction
            # is slew-limited to ±freerun: the net advance stays in [0, 2·freerun],
            # so the phase is monotonic and the waveform never reverses mid-breath.
            # At the H10's sparse effective ACC rate the raw Hilbert edge phase swings
            # by ~π each new sample; without this clamp the oscillator chased it and
            # the bar jittered.  The clamp averages the noisy reference out over many
            # cycles instead of letting any single bad sample yank the phase backward.
            lock = OSC_LOCK_GAIN * max(0.0, min(1.0, (quality - 0.15) / 0.5))
            corr = lock * _wrap_pi(phi_edge - (self._osc_phase + freerun))
            corr = max(-freerun, min(freerun, corr))
            self._osc_phase += freerun + corr
        self._osc_mono = now
        self._osc_phase %= 2.0 * math.pi

        wave_norm = float(math.cos(self._osc_phase))    # +1 at the waveform peak
        phase_2pi = float(self._osc_phase)

        # A rate is only reported once there is enough data AND the spectral peak is
        # clean enough to trust. The low-quality RSA warmup otherwise emits jumpy
        # rates (e.g. 8↔3 br/min) that wobble the readout and reset the resonance
        # lock; the waveform/phase still stream throughout (driven by the oscillator).
        rate_brpm = None
        if freq is not None and span_s >= MIN_RATE_SECONDS and quality >= MIN_RATE_QUALITY:
            rate_brpm = round(freq * 60.0, 1)

        return {
            "method": method,
            "breathing_rate_brpm": rate_brpm,
            "waveform": round(wave_norm, 4),
            "phase_rad": round(phase_2pi, 4),
            "quality": round(quality, 3),
            "amplitude": round(amplitude, 1),
            "window_s": round(span_s, 1),
        }

    # ── Signal builders ────────────────────────────────────────────────────────
    def _build_acc_signal(self) -> Optional[tuple]:
        """Resample the 3 ACC axes, detrend, and project onto PC1. Returns (sig, span_s)."""
        if len(self._acc_t) < 16:
            return None
        t = np.asarray(self._acc_t, dtype=float)
        x = np.asarray(self._acc_x, dtype=float)
        y = np.asarray(self._acc_y, dtype=float)
        z = np.asarray(self._acc_z, dtype=float)
        span_s = float(t[-1] - t[0])

        rx = _resample_uniform(t, x, ACC_FS_RS)
        ry = _resample_uniform(t, y, ACC_FS_RS)
        rz = _resample_uniform(t, z, ACC_FS_RS)
        if rx is None or ry is None or rz is None:
            return None
        _, xi = rx
        _, yi = ry
        _, zi = rz
        m = min(len(xi), len(yi), len(zi))
        M = np.vstack([xi[:m], yi[:m], zi[:m]])

        # Detrend each axis (remove gravity / slow drift), then PCA.
        M = M - M.mean(axis=1, keepdims=True)
        try:
            cov = (M @ M.T) / M.shape[1]
            _, vecs = np.linalg.eigh(cov)      # ascending eigenvalues
            pc1 = vecs[:, -1] @ M              # project onto dominant axis
        except Exception:
            # Fallback: vector magnitude if PCA fails.
            pc1 = np.sqrt(np.sum(M ** 2, axis=0))
            pc1 = pc1 - pc1.mean()
        return pc1, span_s

    def _build_rsa_signal(self) -> Optional[tuple]:
        """Resample the instantaneous-HR tachogram. Returns (sig, span_s)."""
        if len(self._rr_t) < 8:
            return None
        t = np.asarray(self._rr_t, dtype=float)
        hr = np.asarray(self._rr_hr, dtype=float)
        span_s = float(t[-1] - t[0])
        res = _resample_uniform(t, hr, RSA_FS_RS)
        if res is None:
            return None
        _, hri = res
        hri = hri - hri.mean()
        return hri, span_s


# ── Offline self-test ───────────────────────────────────────────────────────────
def _self_test() -> None:
    """Exercise both methods on synthetic data — no hardware required."""
    print("respiration.py self-test")
    print("scipy available:", _HAVE_SCIPY)
    if not _HAVE_SCIPY:
        print("  (install scipy to run the estimator)")
        return

    target_brpm = 6.0
    f = target_brpm / 60.0   # Hz

    # ── ACC: simulate a chest strap breathing at 6 br/min ──────────────────────
    est = RespirationEstimator(method="acc")
    fs = 25.0
    rng = np.random.default_rng(0)
    n_frames = int(40 * fs / 5)          # 40 s of 5-sample frames
    sample_i = 0
    for fi in range(n_frames):
        samples = []
        for _ in range(5):
            ts = sample_i / fs
            breath = 80.0 * math.sin(2 * math.pi * f * ts)   # ±80 mG chest motion
            samples.append({
                "x": 50 + breath + rng.normal(0, 4),
                "y": -990 + 0.3 * breath + rng.normal(0, 4),
                "z": 120 + 0.6 * breath + rng.normal(0, 4),
            })
            sample_i += 1
        frame_ns = int((fi * 5 / fs) * 1e9)
        est.add_acc_frame(frame_ns, fs, samples)
    out = est.estimate()
    print("  ACC  →", out)
    assert out and out["breathing_rate_brpm"] is not None
    assert abs(out["breathing_rate_brpm"] - target_brpm) < 1.0, "ACC rate off"

    # ── RSA: simulate RR intervals modulated at 6 br/min ───────────────────────
    est2 = RespirationEstimator(method="rsa")
    base_rr = 1000.0     # 60 bpm baseline
    t = 0.0
    for _ in range(300):
        # RSA: HR rises on inhale → RR shrinks; modulate RR by ±40 ms at f.
        rr = base_rr - 40.0 * math.sin(2 * math.pi * f * (t / 1000.0))
        est2.add_rr(rr)
        t += rr
    out2 = est2.estimate()
    print("  RSA  →", out2)
    assert out2 and out2["breathing_rate_brpm"] is not None
    assert abs(out2["breathing_rate_brpm"] - target_brpm) < 1.5, "RSA rate off"

    print("  PASS — both methods recovered ~6 br/min")


if __name__ == "__main__":
    _self_test()
