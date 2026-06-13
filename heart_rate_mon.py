#!/usr/bin/env python3
"""
heart_rate_mon.py — Polar H10 BLE monitor → newline-delimited JSON.

Streams heart rate, raw ECG, and accelerometer data from a Polar H10
to stdout and/or a TCP socket.

macOS: Terminal (or the app running this script) must be granted Bluetooth
permission in System Settings → Privacy & Security → Bluetooth.
"""

import argparse
import asyncio
import json
import logging
import os
import struct
import sys
from datetime import datetime, timezone
from typing import Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

try:
    from respiration import RespirationEstimator
except Exception:
    RespirationEstimator = None  # respiration support optional; --resp warns if missing

# ── Standard GATT UUIDs ───────────────────────────────────────────────────────
HR_MEASUREMENT_UUID    = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID     = "00002a19-0000-1000-8000-00805f9b34fb"
BODY_SENSOR_LOC_UUID   = "00002a38-0000-1000-8000-00805f9b34fb"
HR_CONTROL_POINT_UUID  = "00002a39-0000-1000-8000-00805f9b34fb"
MANUFACTURER_NAME_UUID = "00002a29-0000-1000-8000-00805f9b34fb"
MODEL_NUMBER_UUID      = "00002a24-0000-1000-8000-00805f9b34fb"
SERIAL_NUMBER_UUID     = "00002a25-0000-1000-8000-00805f9b34fb"
FIRMWARE_REV_UUID      = "00002a26-0000-1000-8000-00805f9b34fb"
HARDWARE_REV_UUID      = "00002a27-0000-1000-8000-00805f9b34fb"
SOFTWARE_REV_UUID      = "00002a28-0000-1000-8000-00805f9b34fb"

# ── Polar Measurement Data (PMD) — proprietary ────────────────────────────────
PMD_CP_UUID   = "FB005C81-02E7-F387-1CAD-8ACD2D8DF0C8"
PMD_DATA_UUID = "FB005C82-02E7-F387-1CAD-8ACD2D8DF0C8"

PMD_OP_START  = 0x02
PMD_OP_STOP   = 0x03
PMD_TYPE_ECG  = 0x00
PMD_TYPE_ACC  = 0x02

# Nanoseconds between the Polar clock epoch (2000-01-01) and the Unix epoch (1970-01-01)
_POLAR_EPOCH_NS = 946_684_800_000_000_000

BODY_LOCATION_MAP = {
    0: "Other", 1: "Chest", 2: "Wrist", 3: "Finger",
    4: "Hand", 5: "Ear Lobe", 6: "Foot",
}

log = logging.getLogger(__name__)


# ── HR packet parser ──────────────────────────────────────────────────────────
def parse_hr_measurement(data: bytes) -> dict:
    """Parse Heart Rate Measurement GATT characteristic (Bluetooth spec § 3.106)."""
    flags       = data[0]
    hr16        = flags & 0x01
    contact_ok  = (flags >> 1) & 0x03
    energy_pres = bool(flags & 0x08)
    rr_pres     = bool(flags & 0x10)

    idx = 1
    if hr16:
        hr  = int.from_bytes(data[idx:idx+2], "little")
        idx += 2
    else:
        hr  = data[idx]
        idx += 1

    energy: Optional[int] = None
    if energy_pres:
        energy = int.from_bytes(data[idx:idx+2], "little")
        idx   += 2

    rr_ms: list = []
    if rr_pres:
        while idx + 1 <= len(data):
            raw   = int.from_bytes(data[idx:idx+2], "little")
            rr_ms.append(round(raw * 1000 / 1024, 1))  # 1/1024 s → ms
            idx  += 2

    record: dict = {
        "type":           "hr",
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "heart_rate_bpm": hr,
        "contact":        contact_ok in (2, 3),
    }
    if rr_ms:
        record["rr_intervals_ms"] = rr_ms
    if energy is not None:
        record["energy_expended_kj"] = energy
    return record


# ── PMD parsers ───────────────────────────────────────────────────────────────
def _pmd_header(data: bytes) -> tuple:
    """Return (meas_type, unix_ns, frame_type) from a 10-byte PMD packet header."""
    meas_type  = data[0]
    polar_ns   = int.from_bytes(data[1:9], "little")
    frame_type = data[9]
    return meas_type, polar_ns + _POLAR_EPOCH_NS, frame_type


def parse_pmd_ecg(data: bytes) -> dict:
    """
    Parse PMD ECG data frame (frame type 0).
    H10 encodes each sample as 3 bytes (24-bit signed little-endian, 14-bit ADC
    sign-extended to 24 bits).  Values are in microvolts (μV).
    """
    _, unix_ns, frame_type = _pmd_header(data)
    if frame_type != 0:
        raise ValueError(f"Unsupported ECG frame type: {frame_type}")
    payload = data[10:]
    n = len(payload) // 3
    samples = [
        int.from_bytes(payload[i * 3 : i * 3 + 3], byteorder="little", signed=True)
        for i in range(n)
    ]
    return {
        "type":               "ecg",
        "timestamp":          datetime.now(timezone.utc).isoformat(),
        "frame_timestamp_ns": unix_ns,
        "sample_rate_hz":     130,
        "samples_uv":         samples,
    }


def parse_pmd_acc(data: bytes, range_g: int = 8, resolution: int = 14) -> dict:
    """
    Parse PMD accelerometer data frame.

    Both frame types the H10 emits carry raw samples as 3 × int16 LE (6 bytes each):
      frame_type 0 — reference frame.
      frame_type 1 — the H10's normal ACC streaming frame.  The PMD spec describes
                     this as "delta-compressed", but current H10 firmware sends plain
                     int16 samples here.  Validated against raw-byte captures
                     (--debug-acc-raw) at 25 Hz: 36 samples/frame, smooth within each
                     frame and continuous across frame boundaries, gravity magnitude
                     ~1 G.  Higher sample rates were not captured; if a future rate
                     truly delta-compresses, its payload would not be a clean multiple
                     of 6 bytes and the guard below logs it rather than emit garbage.
    Output values are in milliG.  H10 ACC is 14-bit hardware (resolution=14).
    """
    _, unix_ns, frame_type = _pmd_header(data)
    scale   = (range_g * 1000.0) / (1 << (resolution - 1))
    payload = data[10:]
    samples: list = []

    if frame_type in (0, 1):
        n, rem = divmod(len(payload), 6)
        if rem:
            log.warning("ACC frame_type %d: payload %d B is not a multiple of 6 "
                        "(unexpected format — possibly delta-compressed at this rate); "
                        "decoding %d whole int16 samples, %d trailing byte(s) ignored",
                        frame_type, len(payload), n, rem)
        for i in range(n):
            x, y, z = struct.unpack_from("<3h", payload, i * 6)
            samples.append({
                "x": round(x * scale, 2),
                "y": round(y * scale, 2),
                "z": round(z * scale, 2),
            })
    else:
        log.warning("Unsupported ACC frame type 0x%02X — skipping frame", frame_type)

    return {
        "type":               "acc",
        "timestamp":          datetime.now(timezone.utc).isoformat(),
        "frame_timestamp_ns": unix_ns,
        "range_g":            range_g,
        "samples_mg":         samples,
    }


# ── Device information ────────────────────────────────────────────────────────
async def _read_str(client: BleakClient, uuid: str) -> Optional[str]:
    try:
        return (await client.read_gatt_char(uuid)).decode("utf-8", errors="replace").strip()
    except Exception as exc:
        log.debug("Read %s failed: %s", uuid, exc)
        return None


async def read_device_info(client: BleakClient, device_name: str) -> dict:
    fields = {
        "manufacturer":      await _read_str(client, MANUFACTURER_NAME_UUID),
        "model_number":      await _read_str(client, MODEL_NUMBER_UUID),
        "serial_number":     await _read_str(client, SERIAL_NUMBER_UUID),
        "firmware_revision": await _read_str(client, FIRMWARE_REV_UUID),
        "hardware_revision": await _read_str(client, HARDWARE_REV_UUID),
        "software_revision": await _read_str(client, SOFTWARE_REV_UUID),
    }
    rec = {
        "type":      "device_info",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "device":    device_name,
    }
    rec.update({k: v for k, v in fields.items() if v is not None})
    return rec


async def read_body_location(client: BleakClient, device_name: str) -> Optional[dict]:
    try:
        code = (await client.read_gatt_char(BODY_SENSOR_LOC_UUID))[0]
        return {
            "type":          "body_location",
            "timestamp":     datetime.now(timezone.utc).isoformat(),
            "device":        device_name,
            "location_code": code,
            "location":      BODY_LOCATION_MAP.get(code, f"Unknown ({code})"),
        }
    except Exception as exc:
        log.debug("Body sensor location not available: %s", exc)
        return None


# ── PMD command builders ──────────────────────────────────────────────────────
def _pmd_start_cmd(meas_type: int, settings: list) -> bytes:
    """
    Build a PMD START_STREAM command.
    settings: list of (setting_type_byte, uint16_value) pairs.
    """
    cmd = bytearray([PMD_OP_START, meas_type])
    for stype, val in settings:
        cmd += bytes([stype, 0x01]) + val.to_bytes(2, "little")
    return bytes(cmd)


# ECG: 130 Hz, 14-bit resolution (the only mode the H10 supports)
ECG_START_CMD = _pmd_start_cmd(PMD_TYPE_ECG, [(0x00, 130), (0x01, 14)])


def acc_start_cmd(rate_hz: int, range_g: int) -> bytes:
    # H10 only accepts resolution=16 in the PMD command, but the ADC is 14-bit hardware;
    # parse_pmd_acc uses resolution=14 to apply the correct scale factor.
    return _pmd_start_cmd(PMD_TYPE_ACC, [(0x00, rate_hz), (0x01, 16), (0x02, range_g)])


# ── TCP helpers ───────────────────────────────────────────────────────────────
class TCPBroadcaster:
    """Async TCP server — fans out newline-delimited JSON to all connected clients."""

    def __init__(self, host: str, port: int):
        self._host, self._port = host, port
        self._writers: set = set()

    async def start(self):
        server = await asyncio.start_server(self._on_connect, self._host, self._port)
        asyncio.ensure_future(server.serve_forever())
        log.info("TCP server listening on %s:%d", self._host, self._port)

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        addr = writer.get_extra_info("peername")
        log.info("TCP client connected: %s", addr)
        self._writers.add(writer)
        try:
            await reader.read()
        finally:
            self._writers.discard(writer)
            log.info("TCP client disconnected: %s", addr)
            writer.close()

    async def send(self, line: str):
        dead: set = set()
        for w in list(self._writers):
            try:
                w.write((line + "\n").encode())
                await w.drain()
            except OSError:
                dead.add(w)
        self._writers -= dead


class TCPSender:
    """Async TCP client — pushes newline-delimited JSON to a remote server."""

    def __init__(self, host: str, port: int):
        self._host, self._port = host, port
        self._writer: Optional[asyncio.StreamWriter] = None

    async def connect(self):
        _, self._writer = await asyncio.open_connection(self._host, self._port)
        log.info("Connected to TCP server %s:%d", self._host, self._port)

    async def send(self, line: str):
        if self._writer:
            self._writer.write((line + "\n").encode())
            await self._writer.drain()

    async def close(self):
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()


# ── BLE discovery ─────────────────────────────────────────────────────────────
async def discover_devices(timeout: float) -> list:
    return await BleakScanner.discover(timeout=timeout)


async def find_device(name_or_id: Optional[str], timeout: float) -> Optional[BLEDevice]:
    devices = await discover_devices(timeout)
    for d in devices:
        if name_or_id:
            needle = name_or_id.lower()
            if d.address.lower() == needle or (d.name and needle in d.name.lower()):
                return d
        elif d.name and "polar h10" in d.name.lower():
            return d
    return None


# ── Output helper ─────────────────────────────────────────────────────────────
async def emit(record: dict, args, tcp) -> None:
    line = json.dumps(record, indent=2 if args.pretty else None)
    if not args.no_stdout:
        print(line, flush=True)
    if tcp:
        await tcp.send(line)


# ── Main BLE session ──────────────────────────────────────────────────────────
async def run_once(args, tcp) -> None:
    device = await find_device(args.device, args.scan_timeout)
    if device is None:
        label = args.device or "Polar H10"
        sys.exit(f"Device not found: {label}. Try --scan to list nearby BLE devices.")

    log.info("Connecting to %s [%s]…", device.name, device.address)
    device_name = device.name or device.address

    async with BleakClient(device) as client:
        log.info("Connected.")

        # ── respiration estimator (optional) ──────────────────────────────
        resp_est = None
        if args.resp:
            if RespirationEstimator is None:
                log.warning("Respiration requested but respiration.py/scipy unavailable "
                            "\u2014 skipping.")
            else:
                resp_est = RespirationEstimator(method=args.resp_method)
                log.info("Respiration enabled (method=%s, %.1f Hz output).",
                         args.resp_method, args.resp_rate)
                if args.resp_method in ("acc", "auto") and not args.acc:
                    log.warning("Respiration method '%s' uses accelerometer motion; "
                                "without --acc only RSA (HR-based) will be available.",
                                args.resp_method)

        # ── one-shot reads ────────────────────────────────────────────────
        if args.device_info:
            await emit(await read_device_info(client, device_name), args, tcp)

        if args.body_location:
            rec = await read_body_location(client, device_name)
            if rec:
                await emit(rec, args, tcp)

        if args.reset_energy:
            try:
                await client.write_gatt_char(HR_CONTROL_POINT_UUID, bytes([0x01]), response=True)
                log.info("Energy expended counter reset.")
            except Exception as exc:
                log.warning("Reset energy failed: %s", exc)

        # ── battery: read once, then subscribe for live updates ───────────
        battery_pct: list = [None]   # list cell is mutable across closures
        if args.battery:
            try:
                battery_pct[0] = (await client.read_gatt_char(BATTERY_LEVEL_UUID))[0]
                log.info("Battery: %d%%", battery_pct[0])
            except Exception as exc:
                log.debug("Battery read failed: %s", exc)

            def on_battery(_, data: bytes):
                battery_pct[0] = data[0]
                log.info("Battery update: %d%%", battery_pct[0])

            try:
                await client.start_notify(BATTERY_LEVEL_UUID, on_battery)
            except Exception as exc:
                log.debug("Battery notifications not available: %s", exc)

        # ── heart rate notifications ──────────────────────────────────────
        async def on_hr(_, data: bytes):
            record = parse_hr_measurement(data)
            record["device"] = device_name
            if battery_pct[0] is not None:
                record["battery_pct"] = battery_pct[0]
            # Feed RR to the respiration (RSA) estimator. NOT gated on contact: the
            # H10's contact flag reads False even during valid measurement, so the
            # old `and record.get("contact", True)` starved the RSA estimator and it
            # emitted zero resp records. add_rr already plausibility-gates each
            # interval (300–2000 ms), so artifacts are still rejected.
            if resp_est is not None:
                for _rr in record.get("rr_intervals_ms", []):
                    resp_est.add_rr(_rr)
            await emit(record, args, tcp)

        await client.start_notify(HR_MEASUREMENT_UUID, on_hr)

        # ── PMD streams (ECG and/or ACC share one data characteristic) ────
        pmd_started: list = []
        if args.ecg or args.acc:
            acc_range = args.acc_range
            acc_rate  = args.acc_rate

            def on_cp(_, data: bytes):
                if len(data) >= 4 and data[0] == 0xF0:
                    mtype  = data[2]
                    status = data[3]
                    label  = {PMD_TYPE_ECG: "ECG", PMD_TYPE_ACC: "ACC"}.get(mtype, f"0x{mtype:02X}")
                    if status == 0x00:
                        log.info("PMD %s stream: started OK", label)
                    else:
                        log.warning("PMD %s stream error: 0x%02X", label, status)

            async def on_pmd_data(_, data: bytes):
                mtype = data[0]
                try:
                    if mtype == PMD_TYPE_ECG and args.ecg:
                        rec = parse_pmd_ecg(data)
                        rec["device"] = device_name
                        await emit(rec, args, tcp)
                    elif mtype == PMD_TYPE_ACC and args.acc:
                        if args.debug_acc_raw:
                            _, raw_ns, raw_ft = _pmd_header(data)
                            await emit({
                                "type":               "acc_raw",
                                "timestamp":          datetime.now(timezone.utc).isoformat(),
                                "device":             device_name,
                                "frame_type":         raw_ft,
                                "frame_timestamp_ns": raw_ns,
                                "byte_count":         len(data),
                                "payload_hex":        data[10:].hex(),
                                "raw_hex":            data.hex(),
                            }, args, tcp)
                        rec = parse_pmd_acc(data, range_g=acc_range)
                        rec["device"]         = device_name
                        rec["sample_rate_hz"] = acc_rate
                        if resp_est is not None:
                            resp_est.add_acc_frame(rec.get("frame_timestamp_ns"),
                                                   acc_rate, rec.get("samples_mg", []))
                        await emit(rec, args, tcp)
                except Exception as exc:
                    log.warning("PMD parse error (type=0x%02X): %s", mtype, exc)

            pmd_ok = False
            try:
                await client.start_notify(PMD_CP_UUID, on_cp)
                await client.start_notify(PMD_DATA_UUID, on_pmd_data)
                pmd_ok = True
            except Exception as exc:
                log.warning("PMD service not available on this device: %s", exc)

            if pmd_ok:
                if args.ecg:
                    try:
                        await client.write_gatt_char(PMD_CP_UUID, ECG_START_CMD, response=True)
                        log.info("ECG stream started (130 Hz, 14-bit).")
                        pmd_started.append(PMD_TYPE_ECG)
                    except Exception as exc:
                        log.warning("ECG start failed: %s", exc)

                if args.acc:
                    if args.ecg:
                        await asyncio.sleep(0.5)  # let ECG CP settle before starting ACC
                    try:
                        cmd = acc_start_cmd(acc_rate, acc_range)
                        await client.write_gatt_char(PMD_CP_UUID, cmd, response=True)
                        log.info("ACC stream started (%d Hz, ±%dG).", acc_rate, acc_range)
                        pmd_started.append(PMD_TYPE_ACC)
                    except Exception as exc:
                        log.warning("ACC start failed: %s", exc)

        # ── periodic respiration emitter ──────────────────────────────────
        async def _resp_emitter():
            interval = 1.0 / max(0.5, args.resp_rate)
            while True:
                await asyncio.sleep(interval)
                try:
                    est = resp_est.estimate()
                except Exception as exc:
                    log.debug("resp estimate error: %s", exc)
                    continue
                if est is None:
                    continue
                rec = {
                    "type":      "resp",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "device":    device_name,
                    **est,
                }
                await emit(rec, args, tcp)

        resp_task = asyncio.ensure_future(_resp_emitter()) if resp_est is not None else None

        log.info("Streaming — press Ctrl-C to stop.")

        try:
            while client.is_connected:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        finally:
            if resp_task is not None:
                resp_task.cancel()
            try:
                await client.stop_notify(HR_MEASUREMENT_UUID)
            except Exception:
                pass
            for mtype in pmd_started:
                try:
                    await client.write_gatt_char(
                        PMD_CP_UUID, bytes([PMD_OP_STOP, mtype]), response=True
                    )
                except Exception:
                    pass
            if pmd_started:
                for uuid in (PMD_DATA_UUID, PMD_CP_UUID):
                    try:
                        await client.stop_notify(uuid)
                    except Exception:
                        pass


# ── Top-level async entry point ───────────────────────────────────────────────
async def main_async(args) -> None:
    tcp = None
    if args.tcp_port:
        if args.tcp_mode == "server":
            tcp = TCPBroadcaster(args.tcp_host, args.tcp_port)
            await tcp.start()
        else:
            tcp = TCPSender(args.tcp_host, args.tcp_port)
            await tcp.connect()

    if args.scan:
        print("Scanning for BLE devices…", file=sys.stderr)
        devices = await discover_devices(args.scan_timeout)
        if not devices:
            print("No devices found.", file=sys.stderr)
            return
        for d in sorted(devices, key=lambda x: x.name or ""):
            print(f"  {d.address:40s}  {d.name or '(unknown)'}")
        return

    if args.reconnect:
        while True:
            try:
                await run_once(args, tcp)
            except SystemExit:
                raise
            except Exception as exc:
                log.warning("Session ended: %s — reconnecting in 5 s…", exc)
                await asyncio.sleep(5)
    else:
        await run_once(args, tcp)

    if isinstance(tcp, TCPSender):
        await tcp.close()


# ── Output format documentation ───────────────────────────────────────────────
FORMAT_HELP = """\
Output format
=============
All output is newline-delimited JSON.  One JSON object per line (or multi-line
with --pretty).  Every record contains at least three fields:

  type       string   Record type (see below)
  timestamp  string   ISO 8601 UTC wall-clock time the record was received
  device     string   BLE device name or address

Record types
------------

hr  — Heart rate measurement  (always emitted, once per heartbeat)
  heart_rate_bpm       int     Beats per minute
  contact              bool    True when the sensor detects skin contact
  rr_intervals_ms      [float] Beat-to-beat (R-R) intervals in milliseconds
                               Omitted when the sensor does not report them
  energy_expended_kj   int     Cumulative energy since last reset, kilojoules
                               Omitted when the sensor does not report it
  battery_pct          int     Battery level 0–100
                               Present only when --battery is set

ecg  — Raw ECG frame  (requires --ecg)
  sample_rate_hz       int     Always 130 for the Polar H10
  samples_uv           [int]   ECG signal samples in microvolts (μV), oldest first
                               Typically 73 samples per frame at 130 Hz
  frame_timestamp_ns   int     Nanoseconds since Unix epoch, from the device clock

acc  — Accelerometer frame  (requires --acc)
  sample_rate_hz       int     Configured rate: 25 / 50 / 100 / 200 Hz
  range_g              int     Configured range: 2 / 4 / 8 G
  samples_mg           list    3-axis samples, oldest first:
                               [{"x": float, "y": float, "z": float}, ...]
                               Values in milliG (1 G ≈ 9 807 m/s²)
                               Axes: X = lateral, Y = longitudinal, Z = normal
                               to strap (toward the wearer's back when worn on chest)
  frame_timestamp_ns   int     Nanoseconds since Unix epoch, from the device clock

resp  — Derived breathing estimate  (requires --resp)
  method               string  Which source produced this estimate: 'acc' or 'rsa'
  breathing_rate_brpm  float    Breathing rate in breaths per minute
                               (null until ~22 s of data has accumulated)
  waveform             float    Current breathing wave amplitude, normalized -1..1
                               (rising toward +1 ≈ inhale for the RSA method)
  phase_rad            float    Instantaneous breathing phase, 0..2pi (Hilbert)
  quality              float    0..1 confidence (spectral concentration in band)
  amplitude            float    Breathing-signal depth (RMS of the band-passed
                               signal): milliG of chest motion for method 'acc',
                               bpm of HR modulation for method 'rsa'. Indicates
                               whether the sensor is picking up enough signal.
  window_s             float    Seconds of data used for this estimate

acc_raw  — Raw PMD ACC payload, for decoder debugging  (requires --acc --debug-acc-raw)
  frame_type           int     PMD frame type: 0 = reference (raw samples),
                               1 = delta-compressed
  frame_timestamp_ns   int     Nanoseconds since Unix epoch, from the device clock
  byte_count           int     Total length of the raw notification in bytes
  payload_hex          string  Hex of the payload (notification bytes after the
                               10-byte PMD header) — the raw sample/delta data
  raw_hex              string  Hex of the entire notification, header included

device_info  — Firmware and hardware metadata  (requires --device-info)
  manufacturer         string  e.g. "Polar Electro Oy"
  model_number         string  e.g. "H10"
  serial_number        string
  firmware_revision    string
  hardware_revision    string  Omitted if not available
  software_revision    string  Omitted if not available

body_location  — Sensor placement  (requires --body-location)
  location_code        int     Raw GATT Body Sensor Location code
  location             string  Human-readable, e.g. "Chest"

Notes
-----
- frame_timestamp_ns is derived from the Polar device clock (epoch 2000-01-01)
  converted to Unix nanoseconds.  It reflects the start of the data frame, not
  the time of receipt, so it can be used for precise inter-sample timing even
  if the host clock jitters.
- RR intervals use 1/1024 s resolution as defined by the Bluetooth GATT spec,
  which gives ~1 ms precision per interval.
- Energy expended resets at power-off; use --reset-energy to reset it manually.
"""


# ── CLI ───────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="heart_rate_mon",
        description=(
            "Stream Polar H10 heart rate, ECG, and accelerometer data "
            "as newline-delimited JSON to stdout and/or a TCP socket."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Run with --format-help to see a full description of every JSON output field.\n\n"
            "Examples:\n"
            "  heart_rate_mon --scan\n"
            "  heart_rate_mon\n"
            "  heart_rate_mon --ecg --acc --battery --device-info --body-location\n"
            "  heart_rate_mon --ecg --acc --acc-rate 100 --acc-range 4 --pretty\n"
            "  heart_rate_mon --resp --acc --resp-method auto\n"
            "  heart_rate_mon --acc --debug-acc-raw\n"
            "  heart_rate_mon --reset-energy\n"
            "  heart_rate_mon --tcp-port 5555\n"
            "  heart_rate_mon --tcp-port 5555 --no-stdout\n"
            "  heart_rate_mon --tcp-host 192.168.1.10 --tcp-port 5555 --tcp-mode client\n"
            "  heart_rate_mon --reconnect --ecg\n\n"
            "macOS: grant Bluetooth access to Terminal in\n"
            "  System Settings → Privacy & Security → Bluetooth"
        ),
    )

    g = p.add_argument_group("device")
    g.add_argument(
        "--device", metavar="NAME|UUID",
        help=(
            "Target device name substring or CoreBluetooth UUID (macOS) / MAC address (Linux). "
            "Default: first discovered device whose name contains 'Polar H10'."
        ),
    )
    g.add_argument("--scan", action="store_true",
                   help="Scan for nearby BLE devices, print them, then exit.")
    g.add_argument("--scan-timeout", type=float, default=10.0, metavar="SEC",
                   help="BLE scan duration in seconds. (default: 10)")
    g.add_argument("--reconnect", action="store_true",
                   help="Auto-reconnect after a disconnect or error.")

    g = p.add_argument_group("sensors")
    g.add_argument("--ecg", action="store_true",
                   help="Stream raw ECG via Polar PMD service (130 Hz, 14-bit). Emits type='ecg'.")
    g.add_argument("--acc", action="store_true",
                   help="Stream accelerometer via Polar PMD service. Emits type='acc'.")
    g.add_argument("--acc-rate", type=int, default=25, choices=[25, 50, 100, 200],
                   help="Accelerometer sample rate in Hz. (default: 25)")
    g.add_argument("--acc-range", type=int, default=8, choices=[2, 4, 8],
                   help="Accelerometer full-scale range in G. (default: 8)")
    g.add_argument("--battery", action="store_true",
                   help=(
                       "Read battery level at startup and subscribe for updates. "
                       "Adds battery_pct to every hr record."
                   ))
    g.add_argument("--device-info", action="store_true",
                   help="Read and emit device info (firmware, serial, etc.) on connect. Emits type='device_info'.")
    g.add_argument("--body-location", action="store_true",
                   help="Read and emit the Body Sensor Location on connect. Emits type='body_location'.")
    g.add_argument("--reset-energy", action="store_true",
                   help="Write to the HR Control Point to reset the energy-expended accumulator.")

    g = p.add_argument_group("respiration")
    g.add_argument("--resp", action="store_true",
                   help="Derive a breathing signal from ACC and/or RR intervals. Emits type='resp'.")
    g.add_argument("--resp-method", choices=["auto", "acc", "rsa"], default="auto",
                   help=("Breathing source: 'acc' (chest-strap motion via accelerometer), "
                         "'rsa' (respiratory sinus arrhythmia from HR), or 'auto' (ACC when "
                         "streaming, else RSA). (default: auto)"))
    g.add_argument("--resp-rate", type=float, default=5.0, metavar="HZ",
                   help="How often to emit resp records, in Hz. (default: 5)")

    g = p.add_argument_group("debug")
    g.add_argument("--debug-acc-raw", action="store_true",
                   help=("Emit a type='acc_raw' record alongside each ACC frame, carrying "
                         "the raw PMD payload as hex. Used to reverse-engineer and validate "
                         "the frame_type 1 delta decoder. Requires --acc."))

    g = p.add_argument_group("output")
    g.add_argument("--no-stdout", action="store_true",
                   help="Suppress stdout output (useful when using --tcp-port).")
    g.add_argument("--pretty", action="store_true",
                   help="Pretty-print JSON (multi-line indented). Default is one line per record.")
    g.add_argument("--format-help", action="store_true",
                   help="Print a description of all JSON output fields and exit.")

    g = p.add_argument_group("TCP")
    g.add_argument("--tcp-port", type=int, metavar="PORT",
                   help="Enable TCP output. Port to bind (server mode) or connect to (client mode).")
    g.add_argument("--tcp-host", default="127.0.0.1", metavar="HOST",
                   help="TCP bind address (server) or remote host (client). (default: 127.0.0.1)")
    g.add_argument(
        "--tcp-mode", choices=["server", "client"], default="server",
        help=(
            "'server': listen and fan out to all connected clients. "
            "'client': push to an existing TCP server. (default: server)"
        ),
    )

    p.add_argument("--verbose", "-v", action="store_true",
                   help="Enable DEBUG-level logging to stderr.")
    return p


def main():
    args = build_parser().parse_args()

    if args.format_help:
        print(FORMAT_HELP)
        return

    log_level = logging.DEBUG if args.verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)

    log_dir = os.path.join(SCRIPT_DIR, "log")
    os.makedirs(log_dir, exist_ok=True)
    log_filename = datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".log"
    file_handler = logging.FileHandler(os.path.join(log_dir, log_filename), encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)   # always full detail in file
    file_handler.setFormatter(fmt)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.addHandler(stderr_handler)
    root_logger.addHandler(file_handler)

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
