# heart_rate_mon

A **Polar H10** BLE heart rate monitor tool with two interfaces:

- **`monitor_gui.py`** — dark-theme live GUI with scrolling ECG, heart rate, HRV, and accelerometer plots
- **`heart_rate_mon.py`** — CLI that streams all sensor data as newline-delimited JSON to stdout and/or a TCP socket

The CLI is the data engine; the GUI runs it as a subprocess and visualises its output.

---

## Requirements

- macOS (BLE via CoreBluetooth; other platforms may work via bleak but are untested)
- Python 3.11 (installed by `setup.sh` from Homebrew)
- [bleak](https://github.com/hbldh/bleak) ≥ 0.22, matplotlib ≥ 3.7, scipy ≥ 1.10

### Setup

```bash
./setup.sh        # creates .venv, installs dependencies
```

### macOS Bluetooth permission

The terminal (or IDE) running this script must be granted Bluetooth access:

> **System Settings → Privacy & Security → Bluetooth** → enable your terminal app

---

## GUI — `monitor_gui.py`

```bash
./run_gui.sh
```

### Layout

The window is divided into a **sidebar** (left) and a **plot area** (right). At the top of the plot area is a **readout strip** with large live numbers — HR (BPM), HRV (RMSSD ms), breathing (br/min) — plus an inhale/exhale bar and a colour-coded **SIGNAL** readout (breathing `quality` and `amplitude`; green = trustworthy, yellow = marginal, red = too weak). Below it are a large ECG plot and a bottom row of four plots:

| Plot | Description |
|---|---|
| **ECG** | Raw 130 Hz ECG signal in mV, scrolling right-to-left. Glow line overlaid on the trace. |
| **Heart Rate** | BPM over time |
| **HRV** | Beat-to-beat RR interval in ms |
| **Accelerometer** | 3-axis (X/Y/Z) in milliG, 10-second rolling window |
| **Breathing** | Derived breathing waveform with live method + quality in the title |

The **status bar** at the bottom shows: Device · HR · RR · Battery · Contact · ACC mg (live last sample).

**Hover** over any readout, plot, or control to see a tooltip explaining what it shows.

### Sidebar sections

**DEVICE**
- Text field to target a specific device by name substring or BLE address. Leave blank to connect to the first Polar H10 found.

**SENSORS**
- **ECG** — 130 Hz, 14-bit raw ECG (enabled by default)
- **Accelerometer** — 3-axis ACC; configurable rate (25 / 50 / 100 / 200 Hz) and range (2 / 4 / 8 G)
- **Battery level** — reads at connect and subscribes for updates
- **Device info** — logs firmware version, serial number, model on connect
- **Body location** — logs the GATT body sensor placement (always "Chest" for H10)

**BREATHING**
- **Detect breathing** — derive a breathing signal and drive the br/min readout, inhale/exhale bar, SIGNAL readout, and Breathing plot
- **Method** — `auto` (ACC when streaming, else RSA), `acc` (chest motion), or `rsa` (HR variation). ACC needs the Accelerometer enabled; RSA works from heart rate alone.
- **Invert direction** — flips the inhale/exhale bar and waveform. The ACC method's polarity depends on strap orientation, so enable this if the bar moves opposite your breath.

**BEHAVIOUR**
- **Auto-reconnect** — automatically reconnects on drop
- **Reset energy on connect** — sends the HR Control Point reset command (rarely populated by H10)
- **Verbose logging** — enables DEBUG-level output in the log panel and log file

**ECG DISPLAY**
- **Window** — visible ECG timespan in seconds (default 8 s). Edit the field; the plot and buffer update immediately.

**ECG FILTER** — zero-phase filters applied in real time to the ECG display only; raw data is unaffected
- **High-pass 0.5 Hz** — removes baseline wander
- **Low-pass 40 Hz** — removes high-frequency noise
- **Notch 50/60 Hz** — removes power-line interference (select frequency)

**LOG**
- Scrolling log panel showing connection events, errors, and diagnostics
- **Write log to file** — tees the log to a timestamped file in `./log/`
- **Clear log** button

### Connect / Disconnect / Capture

- **Connect** — launches the backend subprocess; clears all plots first
- **Disconnect** — stops the subprocess; plots retain the last session's data until the next Connect
- **Browse / Record** — choose a `.jsonl` file path and toggle capture of raw JSON records to disk

---

## CLI — `heart_rate_mon.py`

```bash
./run.sh [options]          # uses project .venv
# or
python heart_rate_mon.py [options]
```

Streams all H10 sensor data as newline-delimited JSON to stdout and/or a TCP socket.

Writes a DEBUG-level log file to `./log/YYYY-MM-DD_HH-MM-SS.log` on every run.

### Sensors available

| Data | Flag | Record type |
|---|---|---|
| Heart rate + RR intervals + energy | _(always on)_ | `hr` |
| Raw ECG (130 Hz, 14-bit) | `--ecg` | `ecg` |
| 3-axis accelerometer | `--acc` | `acc` |
| Firmware / serial / model | `--device-info` | `device_info` |
| Body sensor placement | `--body-location` | `body_location` |
| Battery level | `--battery` | field in `hr` |
| Derived breathing (ACC / RSA) | `--resp` | `resp` |

### Command-line reference

```
heart_rate_mon [-h] [--format-help] [--verbose]
               [--device NAME|UUID] [--scan] [--scan-timeout SEC] [--reconnect]
               [--ecg] [--acc] [--acc-rate {25,50,100,200}] [--acc-range {2,4,8}]
               [--battery] [--device-info] [--body-location] [--reset-energy]
               [--resp] [--resp-method {auto,acc,rsa}] [--resp-rate HZ]
               [--debug-acc-raw]
               [--no-stdout] [--pretty]
               [--tcp-port PORT] [--tcp-host HOST] [--tcp-mode {server,client}]
```

#### Device

| Flag | Description |
|---|---|
| `--device NAME\|UUID` | Target device by name substring or address. Default: first `Polar H10` found. |
| `--scan` | Scan for nearby BLE devices, print them, then exit. |
| `--scan-timeout SEC` | BLE scan duration in seconds. Default: `10`. |
| `--reconnect` | Auto-reconnect after a disconnect or error. |

#### Sensors

| Flag | Description |
|---|---|
| `--ecg` | Stream raw ECG via the Polar PMD service (130 Hz, 14-bit). Emits `ecg` records. |
| `--acc` | Stream accelerometer via the Polar PMD service. Emits `acc` records. |
| `--acc-rate {25,50,100,200}` | Accelerometer sample rate in Hz. Default: `25`. |
| `--acc-range {2,4,8}` | Accelerometer full-scale range in G. Default: `8`. |
| `--battery` | Read battery level at startup, subscribe for updates; adds `battery_pct` to every `hr` record. |
| `--device-info` | Read and emit firmware/hardware metadata on connect. |
| `--body-location` | Read and emit the body sensor placement on connect. |
| `--reset-energy` | Write to the HR Control Point to reset the cumulative energy-expended counter. |

#### Respiration

Derives a breathing signal from chest-strap motion and/or heart-rate variability. Emits `resp` records (see [Output format](#output-format)).

| Flag | Description |
|---|---|
| `--resp` | Derive a breathing signal. Emits `resp` records. |
| `--resp-method {auto,acc,rsa}` | Source: `acc` (chest-strap accelerometer motion), `rsa` (respiratory sinus arrhythmia from HR), or `auto` (ACC when streaming, else RSA). Default: `auto`. |
| `--resp-rate HZ` | How often to emit `resp` records, in Hz. Default: `5`. |

`acc` needs `--acc`; `rsa` works from heart rate alone. Breathing rate is reliable after ~22 s of data. `quality` (0–1) is the trustworthiness metric; `amplitude` shows how much signal the sensor is picking up.

#### Debug

| Flag | Description |
|---|---|
| `--debug-acc-raw` | Emit an `acc_raw` record (raw PMD payload as hex) alongside each `acc` frame, for decoder debugging. Requires `--acc`. |

#### Output

| Flag | Description |
|---|---|
| `--no-stdout` | Suppress stdout (useful with `--tcp-port`). |
| `--pretty` | Pretty-print JSON (multi-line indented). Default: one compact line per record. |
| `--format-help` | Print full JSON output field documentation and exit. |
| `--verbose`, `-v` | Enable DEBUG-level logging to stderr. |

#### TCP

| Flag | Description |
|---|---|
| `--tcp-port PORT` | Enable TCP output on this port. |
| `--tcp-host HOST` | Bind address (server) or remote host (client). Default: `127.0.0.1`. |
| `--tcp-mode {server,client}` | `server`: fan out to all connected clients. `client`: push to an existing server. Default: `server`. |

### Quick-start examples

```bash
# Find your device address
./run.sh --scan

# Stream heart rate only
./run.sh

# Stream everything
./run.sh --ecg --acc --battery --device-info

# Pretty-print and auto-reconnect
./run.sh --ecg --acc --pretty --reconnect

# High-rate narrow-range accelerometer
./run.sh --acc --acc-rate 200 --acc-range 2

# Derive breathing from chest motion (5 Hz waveform)
./run.sh --acc --resp --resp-method acc

# Breathing from heart rate alone (no ACC needed)
./run.sh --resp --resp-method rsa

# ECG with auto-reconnect, silence log noise
./run.sh --ecg --reconnect 2>/dev/null

# Extract BPM only via jq
./run.sh | jq 'select(.type=="hr") | .heart_rate_bpm'

# TCP server mode — broadcast to all clients
./run.sh --ecg --acc --tcp-port 5555 --no-stdout

# Receive on the other end
nc 127.0.0.1 5555
```

---

## Output format

All output is **newline-delimited JSON** — one object per line (or multi-line with `--pretty`).

Every record has:

| Field | Type | Description |
|---|---|---|
| `type` | string | Record type (see below) |
| `timestamp` | string | ISO 8601 UTC wall-clock time of receipt |
| `device` | string | BLE device name or address |

### `hr` — Heart rate

```json
{
  "type": "hr",
  "timestamp": "2026-06-06T12:00:00.123456+00:00",
  "device": "Polar H10 XXXXXXXX",
  "heart_rate_bpm": 68,
  "contact": true,
  "rr_intervals_ms": [882.8, 879.5],
  "energy_expended_kj": 42,
  "battery_pct": 87
}
```

| Field | Type | Notes |
|---|---|---|
| `heart_rate_bpm` | int | Beats per minute |
| `contact` | bool | `true` when skin contact is detected |
| `rr_intervals_ms` | [float] | Beat-to-beat intervals in ms (omitted when unavailable) |
| `energy_expended_kj` | int | Cumulative energy since last reset, kJ (omitted when unavailable) |
| `battery_pct` | int | 0–100 (present only with `--battery`) |

### `ecg` — Raw ECG

```json
{
  "type": "ecg",
  "timestamp": "2026-06-06T12:00:00.130000+00:00",
  "device": "Polar H10 XXXXXXXX",
  "sample_rate_hz": 130,
  "samples_uv": [14, 22, 31, -8, -120, 980, 420, 55],
  "frame_timestamp_ns": 1749211200000000000
}
```

| Field | Type | Notes |
|---|---|---|
| `sample_rate_hz` | int | Always `130` for the H10 |
| `samples_uv` | [int] | ECG in microvolts (μV), oldest first. ~73 samples per frame at 130 Hz. |
| `frame_timestamp_ns` | int | Device clock in nanoseconds since Unix epoch (Polar epoch offset applied) |

### `acc` — Accelerometer

```json
{
  "type": "acc",
  "timestamp": "2026-06-06T12:00:00.040000+00:00",
  "device": "Polar H10 XXXXXXXX",
  "sample_rate_hz": 25,
  "range_g": 8,
  "samples_mg": [
    {"x": -748.2, "y": -107.4, "z": 544.1},
    {"x": -749.0, "y": -106.8, "z": 543.7}
  ],
  "frame_timestamp_ns": 1749211200000000000
}
```

| Field | Type | Notes |
|---|---|---|
| `sample_rate_hz` | int | Configured rate (`--acc-rate`) |
| `range_g` | int | Configured range (`--acc-range`) |
| `samples_mg` | list | 3-axis samples in milliG, oldest first. A static chest strap reads ~1000 mg total (1G). |
| `frame_timestamp_ns` | int | Device clock in nanoseconds since Unix epoch |

### `resp` — Derived breathing estimate

Requires `--resp`. Emitted at `--resp-rate` (default 5 Hz).

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
  "amplitude": 22.0,
  "window_s": 44.1
}
```

| Field | Type | Notes |
|---|---|---|
| `method` | string | `acc` (chest accelerometer) or `rsa` (HR variability) |
| `breathing_rate_brpm` | float | Breaths per minute. `null` until ~22 s of data. |
| `waveform` | float | Current breathing phase, −1..1, from a phase-locked oscillator. +1 ≈ inhale peak for RSA. |
| `phase_rad` | float | Oscillator phase, 0..2π |
| `quality` | float | 0..1 confidence (spectral concentration in the breathing band). The trustworthiness metric. |
| `amplitude` | float | Breathing-signal depth: milliG of chest motion (`acc`) or bpm of HR modulation (`rsa`). Clean breathing reads ~20 mg; noise floor <~10 mg. |
| `window_s` | float | Seconds of data used for this estimate |

### `acc_raw` — Raw PMD ACC payload (debugging)

Requires `--acc --debug-acc-raw`. One per ACC frame, for reverse-engineering/validating the decoder.

```json
{
  "type": "acc_raw",
  "timestamp": "2026-06-08T01:24:00.000000+00:00",
  "device": "Polar H10 XXXXXXXX",
  "frame_type": 1,
  "frame_timestamp_ns": 1749211200000000000,
  "byte_count": 226,
  "payload_hex": "69fc8100...",
  "raw_hex": "02...69fc8100..."
}
```

| Field | Type | Notes |
|---|---|---|
| `frame_type` | int | PMD frame type (`0` reference, `1` normal streaming) |
| `byte_count` | int | Total notification length |
| `payload_hex` | string | Hex of the payload (bytes after the 10-byte PMD header) |
| `raw_hex` | string | Hex of the entire notification, header included |

### `device_info` — Device metadata

```json
{
  "type": "device_info",
  "timestamp": "2026-06-06T12:00:00.000000+00:00",
  "device": "Polar H10 XXXXXXXX",
  "manufacturer": "Polar Electro Oy",
  "model_number": "H10",
  "serial_number": "XXXXXXXX",
  "firmware_revision": "3.1.1",
  "hardware_revision": "0.0.0"
}
```

### `body_location` — Sensor placement

```json
{
  "type": "body_location",
  "timestamp": "2026-06-06T12:00:00.000000+00:00",
  "device": "Polar H10 XXXXXXXX",
  "location_code": 1,
  "location": "Chest"
}
```

---

## TCP streaming

```bash
# Server: broadcast to all clients on port 5555
./run.sh --ecg --acc --tcp-port 5555 --no-stdout

# Client mode: push to a remote server
./run.sh --ecg --tcp-host 192.168.1.10 --tcp-port 5555 --tcp-mode client

# Receive
nc 127.0.0.1 5555
```

---

## How it works

The H10 exposes two BLE service groups:

**Standard GATT** (Bluetooth SIG-defined):
- Heart Rate Service — HR measurement notifications, body sensor location, HR control point
- Battery Service — battery level reads and notifications
- Device Information Service — manufacturer, model, serial, firmware strings

**Polar Measurement Data (PMD)** (Polar proprietary):
- Control Point characteristic — write `START_STREAM` commands with measurement type and settings
- Data characteristic — receive notification frames containing ECG or ACC samples

ECG and ACC data share the same PMD Data characteristic; the first byte of each frame identifies the measurement type (`0x00` = ECG, `0x02` = ACC).

**ECG framing:** each frame carries ~73 samples (3 bytes each, 24-bit signed LE, 14-bit ADC) at 130 Hz ≈ 562 ms of data per BLE notification.

**ACC framing:** every frame carries raw samples as 3 × int16 LE (6 bytes each) — at 25 Hz, 36 samples (≈1.44 s) per BLE notification. Both PMD frame types the H10 emits (0 and 1) use this same layout; despite the PMD spec describing frame type 1 as "delta-compressed", current H10 firmware sends plain int16 there (verified against raw-byte captures via `--debug-acc-raw`). The H10 ACC is 14-bit hardware regardless of the 16-bit resolution requested in the PMD start command, so values are scaled accordingly. Higher sample rates (50/100/200 Hz) were not validated against raw bytes; if a future rate truly compresses, its payload won't be a clean multiple of 6 and the parser logs a warning rather than emitting garbage.

`frame_timestamp_ns` in ECG and ACC records uses the device's internal clock (Polar epoch: 2000-01-01 00:00:00 UTC, converted to Unix nanoseconds). This can be used for precise inter-sample timing independent of host clock jitter.
