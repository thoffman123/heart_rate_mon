# heart_rate_mon — Claude Code working context

This file captures active development context for Claude Code. The README covers the public interface; this covers internal state, active bugs, design decisions, and pending work.

## What this project is and where it fits

A Polar H10 BLE monitor with two interfaces: a CLI (`heart_rate_mon.py`) that streams newline-delimited JSON, and a Tkinter GUI (`monitor_gui.py`) that runs the CLI as a subprocess and plots its output live. This project is the **sensor layer** of a biofeedback meditation system. The **experience layer** is a new Python meditation app (`../meditation_app/`) currently being built.

In the target architecture, one CLI instance runs in TCP server mode and fans out the same data stream to two simultaneous consumers:
1. The **meditation app** — primary consumer, launches the CLI at session start
2. The **monitor GUI** — diagnostic companion, launched on demand from the meditation app's config menu, connects as a TCP client

## How to run

```bash
source .venv/bin/activate    # Python 3.11, bleak + matplotlib + scipy
./run_gui.sh                 # GUI (standalone, spawns its own CLI subprocess)
./run.sh --resp --ecg --acc  # CLI with breathing detection
```

The `.venv` is already built. Don't recreate it.

## TCP fan-out architecture (target)

The meditation app launches the CLI in TCP server mode:

```
heart_rate_mon.py --tcp-port 5555 --resp --acc --ecg  (TCP server)
      ├──→  meditation_app   (TCP client)
      └──→  monitor_gui.py   (TCP client, launched on demand)
```

The current GUI always spawns its own CLI subprocess. It needs a second mode: **connect to an existing TCP stream** rather than launching a new process. The `--tcp-mode client` flag already exists in the CLI; the GUI needs a corresponding UI path (e.g. a "Connect to running session" option in the config or a TCP host/port field alongside the existing Connect button).

## File map

| File | What it does |
|---|---|
| `heart_rate_mon.py` | CLI data engine. Connects via BLE, streams all H10 sensor data as JSON. Manages the `--resp` emitter. |
| `respiration.py` | `RespirationEstimator` — stateful class that takes ACC frames and RR intervals and produces a breathing estimate. Has an extensive inline design doc. Run directly (`python respiration.py`) for a self-test on synthetic data. |
| `monitor_gui.py` | Tkinter dark-theme GUI. Spawns the CLI as a subprocess, parses JSON, plots ECG / HR / HRV / ACC / breathing. Inhale/exhale bar widget in the top strip. |

## The `resp` record (key schema — not in README)

Emitted by the `--resp` flag at 5 Hz (configurable via `--resp-rate`). The GUI parses it; the meditation app consumes it too.

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
- `amplitude`: breathing-signal depth (RMS of the band-passed signal). milliG for `acc`, bpm for `rsa`. Quality is the trustworthiness metric; amplitude alone is a weak discriminator.
- `breathing_rate_brpm`: null until ~22 s of data; reliable after that.

## Breathing detection architecture

### respiration.py — RespirationEstimator

Three internal stages:
1. **Buffer** — ACC and RR data collected in rolling 45-second deques.
2. **Signal build** — ACC: resample 3 axes to 10 Hz → detrend → PCA (orientation-independent). RSA: instantaneous-HR tachogram from RR → resample to 4 Hz → detrend.
3. **Estimate** — Bandpass (0.05–0.7 Hz) → FFT peak + parabolic interpolation → rate. Hilbert transform (backed off from noisy buffer edge by 0.8 s) → phase. Phase-locked oscillator generates the smooth waveform output.

The **phase-locked oscillator** was added to fix a "bar sat in the middle" bug: the Hilbert/filter edge artifacts at the newest sample made `filtered[-1]` collapse to zero most of the time when ACC was sparse. The oscillator free-runs at the detected rate and gently corrects toward the measured phase. The `estimate(now=)` param accepts an injected clock for offline replay.

### heart_rate_mon.py — `--resp` flag

- Adds `--resp`, `--resp-method {auto,acc,rsa}`, `--resp-rate HZ` flags.
- Instantiates `RespirationEstimator` and starts `_resp_emitter()` async task.
- `on_hr` feeds RR intervals; `on_pmd_data` ACC branch feeds frames at full 25 Hz (decoder fixed).
- Emits `{"type":"resp", ...}` records at the configured rate.

### monitor_gui.py — GUI breathing display

- Breathing sidebar section: enable/disable checkbox + method combobox.
- Top strip: large cyan br/min readout + inhale/exhale vertical bar (34×104 px canvas, `_build_breath_bar` / `_draw_breath_bar`).
- 4th bottom-row plot: scrolling waveform with live method + quality in title.

## Known bugs and current workarounds

### ACC decoder (frame_type 1)  ← FIXED 2026-06-08

The old `parse_pmd_acc` misidentified frame_type 1 as Polar delta-compressed, producing garbage values. An over-range guard masked it by keeping only the first sample per frame → ~0.65 Hz effective ACC. **Fixed:** frame_type 1 is plain `3 × int16 LE` samples (identical to frame_type 0). Full 25 Hz restored; breathing quality rose to ~0.89. Validated at 25 Hz only — higher rates not yet captured.

### ACC polarity (inhale/exhale direction)  ← manual workaround in place

For RSA, +waveform is physiologically anchored to inhale. For ACC, the PCA eigenvector sign is arbitrary — the bar can be inverted depending on strap orientation.

**Current fix:** manual **Invert direction** checkbox in the GUI BREATHING section (`opt_resp_invert`). Reliable and immediate.

**Auto-resolution deferred:** RSA-anchored correlation requires a shared clock between the ACC (device clock) and RR (beat-accumulated clock) streams. Breath-asymmetry fallback (derivative skewness) was tested and proved unreliable. Manual toggle is the right answer for now.

## Pending work

1. **ACC decoder** — ✅ done 2026-06-08.
2. **`--debug-acc-raw` flag** — ✅ done. Emits `type='acc_raw'` records with raw payload hex.
3. **ACC polarity auto-resolver** — deferred; manual toggle in place.
4. **Auto method picks higher quality** — `_choose_method` currently always prefers ACC when live. Could compare `quality` scores and pick the better one each cycle. One-liner change.
5. **TCP client mode for monitor GUI** — add a "connect to existing stream" path so the GUI can attach to a CLI instance launched by the meditation app. The CLI `--tcp-mode client` flag already exists; the GUI needs corresponding UI.
6. **Feed meditation_app** — the meditation app connects to the CLI's TCP server (`--tcp-port`) and consumes `hr` + `resp` records to drive visuals and audio.

## Design principles to keep

- Feedback should be **ambient, not gamified**. Reward letting go, not trying harder.
- Use **slow time constants** that match the meditation app's aesthetic.
- The `resp` record is the public API. The estimator internals can change; the schema should stay stable.
- All changes validated with `py_compile` + `python respiration.py` self-test before delivery.

## Longer-term biofeedback roadmap

See `../meditation_app/CLAUDE.md` and `../meditation_app/docs/biofeedback-research-and-ideas.md` for the full Tier 1–3 feature roadmap.

---

## Platform strategy

**Current target: Mac, Python.** The existing stack (bleak + asyncio CLI, Tkinter GUI) is the right tool for the Mac. The meditation app is also Python, keeping the entire system in one language and runtime. No plans to change the CLI or GUI stack.

**Sensor: Polar H10 only.** Apple Watch was evaluated and rejected — direct BLE to Mac, dedicated ECG chip for RR accuracy, and chest-strap positioning for ACC breathing detection make the H10 the right choice for this use case.

**Future platforms: iOS, iPadOS, Android.** Python + bleak has no path to mobile. Recommended approach when ready:

- **Flutter (Dart)** — single codebase for iOS, iPadOS, Android, macOS; `flutter_blue_plus` for BLE.
- **Do not start a mobile rewrite until the Mac experience is complete and validated.**

**Python as reference implementation.** `RespirationEstimator` is intentionally dependency-light (numpy + scipy only). The FFT, PCA, bandpass, Hilbert, and phase-locked oscillator all have direct equivalents in any platform's DSP library. The `resp` schema is the stable cross-platform API — the meditation app consumes identical records regardless of what language produces them.

**BLE on mobile.** The H10 presents identical GATT services on mobile. The `parse_pmd_acc` decoder (plain int16 triplets, `res=14` scale) and `parse_ecg` are validated and port directly.
