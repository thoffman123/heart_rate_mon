# heart_rate_mon — Claude Code working context

This file captures active development context for Claude Code. The README covers the public interface; this covers internal state, active bugs, design decisions, and pending work.

## What this project is and where it fits

A Polar H10 BLE monitor with two interfaces: a CLI (`heart_rate_mon.py`) that streams newline-delimited JSON, and a Tkinter GUI (`monitor_gui.py`) that runs the CLI as a subprocess and plots its output live. The project is the **data engine** for a separate meditation timer web app (`../meditation_app/`) — specifically, the `resp` record (see below) will eventually feed that app's visual and audio biofeedback layer.

## How to run

```bash
source .venv/bin/activate    # Python 3.11, bleak + matplotlib + scipy
./run_gui.sh                 # GUI
./run.sh --resp --ecg --acc  # CLI with breathing detection
```

The `.venv` is already built. Don't recreate it.

## File map

| File | What it does |
|---|---|
| `heart_rate_mon.py` | CLI data engine. Connects via BLE, streams all H10 sensor data as JSON. Manages the `--resp` emitter. |
| `respiration.py` | `RespirationEstimator` — stateful class that takes ACC frames and RR intervals and produces a breathing estimate. Has an extensive inline design doc. Run directly (`python respiration.py`) for a self-test on synthetic data. |
| `monitor_gui.py` | Tkinter dark-theme GUI. Spawns the CLI as a subprocess, parses JSON, plots ECG / HR / HRV / ACC / breathing. Inhale/exhale bar widget in the top strip. |

## The `resp` record (key schema — not in README)

Emitted by the `--resp` flag at 5 Hz (configurable via `--resp-rate`). The GUI parses it; the meditation app will eventually consume it too.

```json
{
  "type": "resp",
  "timestamp": "2026-06-08T01:24:00.000000+00:00",
  "device": "Polar H10 XXXXXXXX",
  "method": "acc",
  "breathing_rate_brpm": 6.0,
  "waveform": 0.82,
  "phase_rad": 1.35,
  "quality": 0.91,
  "window_s": 44.1
}
```

- `method`: `"acc"` (chest accelerometer) or `"rsa"` (respiratory sinus arrhythmia from HR)
- `waveform`: −1..1, current breathing phase via phase-locked oscillator. +1 ≈ inhale peak for RSA; sign is ambiguous for ACC (see polarity section below).
- `phase_rad`: 0..2π, oscillator phase.
- `quality`: 0..1, spectral concentration at the dominant breathing frequency.
- `amplitude`: breathing-signal depth (RMS of the band-passed signal). milliG of chest motion for `acc`, bpm of HR modulation for `rsa`. Surfaces whether the sensor is picking up signal — clean meditative breathing reads ~20 mg (and scores quality ~0.9); the noise floor is <~10 mg. Note: amplitude alone is a weak discriminator (12 mg can be junk, 20 mg can be excellent); `quality` is the trustworthiness metric.
- `breathing_rate_brpm`: null until ~22 s of data; reliable after that.

## Breathing detection architecture

### respiration.py — RespirationEstimator

Three internal stages:
1. **Buffer** — ACC and RR data collected in rolling 45-second deques.
2. **Signal build** — ACC: resample 3 axes to 10 Hz → detrend → PCA (orientation-independent). RSA: instantaneous-HR tachogram from RR → resample to 4 Hz → detrend.
3. **Estimate** — Bandpass (0.05–0.7 Hz) → FFT peak + parabolic interpolation → rate. Hilbert transform (backed off from noisy buffer edge by 0.8 s) → phase. Phase-locked oscillator generates the smooth waveform output.

The **phase-locked oscillator** was added to fix a "bar sat in the middle" bug: the Hilbert/filter edge artifacts at the newest sample made `filtered[-1]` collapse to zero most of the time when ACC was sparse. The oscillator free-runs at the detected rate and gently corrects toward the measured phase — smooth even at 0.65 Hz effective ACC rate. The `estimate(now=)` param accepts an injected clock for offline replay (tested against real capture data).

### heart_rate_mon.py — `--resp` flag

- Adds `--resp`, `--resp-method {auto,acc,rsa}`, `--resp-rate HZ` flags.
- Instantiates `RespirationEstimator` and starts `_resp_emitter()` async task.
- `on_hr` feeds RR intervals; `on_pmd_data` ACC branch feeds frames (full 25 Hz now that the decoder is fixed — see below).
- Emits `{"type":"resp", ...}` records at the configured rate.

### monitor_gui.py — GUI breathing display

- Breathing sidebar section: enable/disable checkbox + method combobox.
- Top strip: large cyan br/min readout + inhale/exhale vertical bar (34×104 px canvas, `_build_breath_bar` / `_draw_breath_bar`) + colour-coded SIGNAL readout (quality 0–1 and amplitude, green/yellow/red, `_update_signal_readout`) so the user can see whether the sensor has enough signal before trusting/recording.
- 4th bottom-row plot: scrolling waveform with live method + quality in title.

## Known bugs and current workarounds

### ACC decoder (frame_type 1)  ← FIXED 2026-06-08

**This was the root cause of the breathing-waveform jitter.** The old `parse_pmd_acc` assumed frame_type 1 was Polar delta-compressed (reference sample + packed bit-deltas) and produced garbage (1e13–1e23 mg); an over-range guard masked it by keeping only the first sample per frame → ~0.65 Hz effective ACC instead of 25 Hz. At that rate the Hilbert edge phase is unrecoverable, so the real-time waveform jittered.

**The actual format:** A `--debug-acc-raw` capture (`capture/acc_raw.jsonl`, 25 Hz) proved frame_type 1 is **not** delta-compressed — it carries plain `3 × int16 LE` samples (6 bytes each), identical encoding and scale to frame_type 0. 36 samples/frame at 25 Hz (216-byte payload), smooth within each frame and continuous across boundaries, gravity magnitude ~0.95 G with the existing `res=14` scale. `parse_pmd_acc` now decodes both frame types as int16 triplets. The `_read_signed_bits` helper and the over-range guards were removed.

**Scope:** Validated at 25 Hz only. Higher rates (50/100/200 Hz) weren't captured; if a future rate truly delta-compresses, its payload won't be a clean multiple of 6 bytes — `parse_pmd_acc` logs a warning and decodes whole samples rather than emitting garbage. 25 Hz is the GUI default and is plenty for breathing.

**Result:** Breathing `quality` rose from ~0.3–0.5 to ~0.89; the waveform is smooth on its own. The slew-limited oscillator phase lock (below) is now belt-and-suspenders rather than load-bearing.

### ACC polarity (inhale/exhale direction)  ← pending

For the RSA method, +waveform is physiologically anchored to inhale (HR rises on inhale). For ACC, the PCA eigenvector sign is arbitrary — so the inhale/exhale bar might be inverted depending on strap orientation.

**Planned fix (not yet implemented):**
1. Primary: correlate the ACC waveform with the RSA waveform over a window. If anti-correlated, flip the ACC waveform sign. RSA is the ground-truth anchor.
2. Fallback (ACC-only): breath-cycle asymmetry — inhalation is typically faster than exhalation, so the rising slope of the waveform is steeper. Measure skewness of the derivative; positive direction = inhale.
3. Expose the resolved polarity in the `resp` record once implemented.

## Pending work

1. **`--debug-acc-raw` flag** — ✅ **done.** Emits a `type='acc_raw'` record alongside each ACC frame (`frame_type`, `frame_timestamp_ns`, `byte_count`, `payload_hex`, `raw_hex`). Capture 20-30 s with `--acc --debug-acc-raw` and feed it to the decoder rewrite (item 2). Documented in `--format-help`.

2. **Proper ACC decoder** — ✅ **done 2026-06-08.** frame_type 1 is plain int16 triplets, not delta-compressed (see Known bugs above). Full 25 Hz restored; breathing quality ~0.89.

3. **ACC polarity resolver** — RSA-anchored correlation + asymmetry fallback as described above.

4. **Auto method picks higher quality** — current auto always prefers ACC when live. Could instead pick whichever of ACC/RSA reports higher `quality` on each estimate cycle. Simple one-liner in `_choose_method`.

5. **Feed meditation_app** — the resp stream needs to reach the web app. Options: TCP socket (already supported by the CLI), a small WebSocket bridge, or a local HTTP endpoint. The web app consumes it to drive `u_breathe` and other shader uniforms.

## Design principles to keep

- Feedback should be **ambient, not gamified**. Reward letting go, not trying harder.
- Use **slow time constants** that match the meditation app's aesthetic.
- The `resp` record is the public API. The estimator internals can change; the schema should stay stable.
- All changes validated with `py_compile` + `python respiration.py` self-test before delivery.

## Longer-term biofeedback roadmap

See `../meditation_app/CLAUDE.md` and `../meditation_app/docs/biofeedback-research-and-ideas.md` for the full Tier 1–3 feature roadmap. The H10 tooling is the sensor layer; the meditation app is the experience layer.
