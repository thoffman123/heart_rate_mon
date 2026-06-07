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

The window is divided into a **sidebar** (left) and **four live plots** (right):

| Plot | Description |
|---|---|
| **ECG** | Raw 130 Hz ECG signal in mV, scrolling right-to-left. Glow line overlaid on the trace. |
| **Heart Rate** | BPM over time |
| **HRV** | Beat-to-beat RR interval in ms |
| **Accelerometer** | 3-axis (X/Y/Z) in milliG, 10-second rolling window |

The **status bar** at the bottom shows: Device · HR · RR · Battery · Contact · ACC mg (live last sample).

### Sidebar sections

**DEVICE**
- Text field to target a specific device by name substring or BLE address. Leave blank to connect to the first Polar H10 found.

**SENSORS**
- **ECG** — 130 Hz, 14-bit raw ECG (enabled by default)
- **Accelerometer** — 3-axis ACC; configurable rate (25 / 50 / 100 / 200 Hz) and range (2 / 4 / 8 G)
- **Battery level** — reads at connect and subscribes for updates
- **Device info** — logs firmware version, serial number, model on connect
- **Body location** — logs the GATT body sensor placement (always "Chest" for H10)

**BEHAVIOUR**
- **Auto-reconnect** — automatically reconnects on drop
- **Reset energy on connect** — sends the HR Control Point reset command (rarely populated by H10)
- **Verbose logging** — enables DEBUG-level output in the log panel and log file

**ECG DISPLAY**
- **Window** — visible ECG timespan in seconds (default 8 s). Edit and press **Apply window**.

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

### Command-line reference

```
heart_rate_mon [-h] [--format-help] [--verbose]
               [--device NAME|UUID] [--scan] [--scan-timeout SEC] [--reconnect]
               [--ecg] [--acc] [--acc-rate {25,50,100,200}] [--acc-range {2,4,8}]
               [--battery] [--device-info] [--body-location] [--reset-energy]
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

**ACC framing:** the first frame after connect is raw (frame type 0, 3 × int16 LE per sample). Subsequent frames use Polar delta compression (frame type 1: 6-byte reference sample + 1-byte delta size + packed signed deltas). The H10 ACC is 14-bit hardware regardless of the 16-bit resolution requested in the PMD start command.

`frame_timestamp_ns` in ECG and ACC records uses the device's internal clock (Polar epoch: 2000-01-01 00:00:00 UTC, converted to Unix nanoseconds). This can be used for precise inter-sample timing independent of host clock jitter.
