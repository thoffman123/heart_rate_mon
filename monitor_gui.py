#!/usr/bin/env python3
"""
monitor_gui.py — Real-time GUI for the Polar H10 BLE monitor.

Spawns heart_rate_mon.py as a subprocess and displays ECG, heart rate,
HRV, accelerometer, and derived breathing data as live scrolling plots.
All incoming JSON can be captured to a file without interrupting the display.
"""

import collections
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from scipy.signal import butter, sosfilt, sosfiltfilt, iirnotch, tf2sos
from matplotlib.ticker import MultipleLocator, FuncFormatter, NullFormatter

# ── paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
MONITOR_SCRIPT = os.path.join(SCRIPT_DIR, "heart_rate_mon.py")
PYTHON         = sys.executable

# ── buffer / display config ───────────────────────────────────────────────────
ECG_RATE    = 130
ECG_WIN_DEF = 8.0
ECG_MAXLEN  = int(ECG_WIN_DEF * ECG_RATE * 3)
HR_MAXLEN   = 300
ACC_MAXLEN  = 1000
ACC_WIN_S   = 10.0
HR_WIN_S    = 120.0
RR_ROLLING  = 60      # RR intervals kept in rolling RMSSD window
POLL_MS     = 50
MAX_DRAIN   = 400
RESP_MAXLEN = 1200    # breathing waveform samples kept (~4 min at 5 Hz)
RESP_WIN_S  = 90.0    # breathing plot window (seconds)

# ── colours ───────────────────────────────────────────────────────────────────
BG_ROOT  = "#12121f"
BG_PANEL = "#1a1a2e"
BG_PLOT  = "#0d0d1a"
BG_ENTRY = "#222240"
FG       = "#d0d0e0"
FG_DIM   = "#8888aa"
CYAN     = "#00e5ff"
GREEN    = "#69f0ae"
RED      = "#ff5252"
YELLOW   = "#ffd740"
BLUE     = "#448aff"
ORANGE   = "#ff9100"
MAGENTA  = "#ea80fc"
ACCENT   = "#e040fb"

# ── fonts ─────────────────────────────────────────────────────────────────────
F_XS   = ("Helvetica", 12)
F_SM   = ("Helvetica", 13)
F_MD   = ("Helvetica", 14)
F_LG   = ("Helvetica", 15, "bold")
F_BTN  = ("Helvetica", 14, "bold")
F_SEC  = ("Helvetica", 13, "bold")    # sidebar section header
F_MONO = ("Courier", 11)

matplotlib.rcParams.update({
    "text.color":        FG,
    "axes.labelcolor":   FG_DIM,
    "xtick.color":       FG_DIM,
    "ytick.color":       FG_DIM,
    "axes.edgecolor":    "#2a2a3e",
    "grid.color":        "#1e1e30",
    "grid.linewidth":    0.5,
    "legend.facecolor":  BG_PANEL,
    "legend.edgecolor":  "#2a2a3e",
    "legend.labelcolor": FG,
    "figure.facecolor":  BG_ROOT,
    "axes.facecolor":    BG_PLOT,
})


# ── HRV helper ────────────────────────────────────────────────────────────────
def _rmssd(rr_vals: list) -> Optional[float]:
    """Root Mean Square of Successive Differences from a list of RR intervals (ms)."""
    if len(rr_vals) < 2:
        return None
    diffs = [rr_vals[i + 1] - rr_vals[i] for i in range(len(rr_vals) - 1)]
    return math.sqrt(sum(d * d for d in diffs) / len(diffs))


class MonitorApp:
    # ── init ──────────────────────────────────────────────────────────────────
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Polar H10 Monitor")
        self.root.configure(bg=BG_ROOT)
        self.root.geometry("1360x900")
        self.root.minsize(960, 660)

        self.proc: Optional[subprocess.Popen] = None
        self.data_queue: queue.Queue = queue.Queue()

        # data buffers
        self.ecg_buf:    collections.deque = collections.deque(maxlen=ECG_MAXLEN)
        self.hr_t:       collections.deque = collections.deque(maxlen=HR_MAXLEN)
        self.hr_v:       collections.deque = collections.deque(maxlen=HR_MAXLEN)
        self.hrv_t:      collections.deque = collections.deque(maxlen=HR_MAXLEN)
        self.hrv_v:      collections.deque = collections.deque(maxlen=HR_MAXLEN)
        self.rr_rolling: collections.deque = collections.deque(maxlen=RR_ROLLING)
        self.acc_t:      collections.deque = collections.deque(maxlen=ACC_MAXLEN)
        self.acc_x:      collections.deque = collections.deque(maxlen=ACC_MAXLEN)
        self.acc_y:      collections.deque = collections.deque(maxlen=ACC_MAXLEN)
        self.acc_z:      collections.deque = collections.deque(maxlen=ACC_MAXLEN)
        self.resp_t:     collections.deque = collections.deque(maxlen=RESP_MAXLEN)
        self.resp_v:     collections.deque = collections.deque(maxlen=RESP_MAXLEN)
        self._resp_method  = ""
        self._resp_quality: Optional[float] = None
        self._resp_amplitude: Optional[float] = None

        self.connect_time: Optional[float] = None
        self.capture_file  = None
        self.capturing     = False
        self._cappath      = ""
        self._pkt_count    = 0
        self._paused       = False

        # StringVars — status bar + large readouts
        self.sv_status  = tk.StringVar(value="Disconnected")
        self.sv_hr      = tk.StringVar(value="—")
        self.sv_hr_num  = tk.StringVar(value="—")
        self.sv_hrv_num = tk.StringVar(value="—")
        self.sv_br_num  = tk.StringVar(value="—")
        self.sv_quality = tk.StringVar(value="—")
        self.sv_amp     = tk.StringVar(value="—")
        self.sv_rr      = tk.StringVar(value="—")
        self.sv_battery = tk.StringVar(value="—")
        self.sv_contact = tk.StringVar(value="—")
        self.sv_device  = tk.StringVar(value="—")
        self.sv_capfile = tk.StringVar(value="no file selected")
        self.sv_pkts    = tk.StringVar(value="")
        self.sv_acc     = tk.StringVar(value="—")

        # sensor / behaviour options
        self.opt_device       = tk.StringVar(value="")
        self.opt_ecg          = tk.BooleanVar(value=True)
        self.opt_acc          = tk.BooleanVar(value=True)
        self.opt_acc_rate     = tk.StringVar(value="25")
        self.opt_acc_range    = tk.StringVar(value="8")
        self.opt_battery      = tk.BooleanVar(value=True)
        self.opt_device_info  = tk.BooleanVar(value=True)
        self.opt_body_loc     = tk.BooleanVar(value=True)
        self.opt_resp         = tk.BooleanVar(value=True)
        self.opt_resp_method  = tk.StringVar(value="auto")
        self.opt_reconnect    = tk.BooleanVar(value=False)
        self.opt_reset_energy = tk.BooleanVar(value=False)
        self.opt_ecg_win      = tk.DoubleVar(value=ECG_WIN_DEF)
        self.opt_verbose      = tk.BooleanVar(value=False)
        self.opt_log_file     = tk.BooleanVar(value=False)
        self._log_fh          = None   # open file handle when tee is active

        # ECG filter options
        self.opt_filter_hp    = tk.BooleanVar(value=True)
        self.opt_filter_lp    = tk.BooleanVar(value=True)
        self.opt_filter_notch = tk.BooleanVar(value=True)
        self.opt_notch_freq   = tk.StringVar(value="60")
        self.sos_hp           = None
        self.sos_lp           = None
        self.sos_notch        = None
        self._build_filters()

        self._build_ui()
        self.root.after(200, self._initial_draw)
        self._poll()

    # ── layout ────────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self._build_statusbar()
        self._build_toolbar()
        self._build_body()

    def _build_toolbar(self) -> None:
        bar = tk.Frame(self.root, bg=BG_PANEL, height=54)
        bar.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(4, 0))
        bar.pack_propagate(False)

        self.btn_connect = tk.Button(
            bar, text="▶  Connect", command=self._connect,
            bg=GREEN, fg="#001a0a", font=F_BTN,
            relief=tk.FLAT, padx=14, cursor="hand2",
            activebackground=GREEN, activeforeground="#001a0a",
        )
        self.btn_connect.pack(side=tk.LEFT, padx=(10, 4), pady=8)

        self.btn_disconnect = tk.Button(
            bar, text="■  Disconnect", command=self._disconnect,
            bg=BG_ENTRY, fg=FG, font=F_MD,
            relief=tk.FLAT, padx=14, cursor="hand2",
            activebackground=BG_ENTRY, activeforeground=FG,
            state=tk.DISABLED,
        )
        self.btn_disconnect.pack(side=tk.LEFT, padx=(0, 4), pady=8)

        self.btn_pause = tk.Button(
            bar, text="⏸  Pause", command=self._toggle_pause,
            bg=BG_ENTRY, fg=FG, font=F_MD,
            relief=tk.FLAT, padx=14, cursor="hand2",
            activebackground=BG_ENTRY, activeforeground=FG,
            state=tk.DISABLED,
        )
        self.btn_pause.pack(side=tk.LEFT, padx=(0, 20), pady=8)

        tk.Label(
            bar, textvariable=self.sv_status,
            bg=BG_PANEL, fg=YELLOW, font=F_LG,
        ).pack(side=tk.LEFT, padx=6)

        tk.Label(
            bar, textvariable=self.sv_pkts,
            bg=BG_PANEL, fg=FG_DIM, font=F_SM,
        ).pack(side=tk.LEFT, padx=10)

        # recording — right side
        tk.Button(
            bar, text="Browse…", command=self._browse_capture,
            bg=BG_ENTRY, fg=FG, font=F_SM,
            relief=tk.FLAT, padx=10, cursor="hand2",
        ).pack(side=tk.RIGHT, padx=(4, 10), pady=8)

        tk.Label(
            bar, textvariable=self.sv_capfile,
            bg=BG_PANEL, fg=CYAN, font=F_XS,
        ).pack(side=tk.RIGHT, padx=4)

        self.btn_record = tk.Button(
            bar, text="⏺  Record", command=self._toggle_capture,
            bg=BG_ENTRY, fg=FG, font=F_MD,
            relief=tk.FLAT, padx=12, cursor="hand2",
            activebackground=BG_ENTRY, activeforeground=FG,
        )
        self.btn_record.pack(side=tk.RIGHT, padx=4, pady=8)

    def _build_body(self) -> None:
        body = tk.Frame(self.root, bg=BG_ROOT)
        body.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        sidebar = self._build_sidebar(body)
        sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 4))
        plots = self._build_plots(body)
        plots.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    def _build_sidebar(self, parent: tk.Widget) -> tk.Widget:
        outer = tk.Frame(parent, bg=BG_PANEL, width=260)
        outer.pack_propagate(False)

        cvs = tk.Canvas(outer, bg=BG_PANEL, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=cvs.yview)
        cvs.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        cvs.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        inner = tk.Frame(cvs, bg=BG_PANEL)
        win = cvs.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: cvs.configure(scrollregion=cvs.bbox("all")))
        cvs.bind("<Configure>",   lambda e: cvs.itemconfig(win, width=e.width))

        P = dict(fill=tk.X, padx=8, pady=3)

        # ── Device ────────────────────────────────────────────────────────
        self._sec(inner, "DEVICE")
        tk.Label(inner, text="Name / UUID  (blank = auto)",
                 bg=BG_PANEL, fg=FG_DIM, font=F_XS, anchor="w").pack(**P)
        tk.Entry(inner, textvariable=self.opt_device,
                 bg=BG_ENTRY, fg=FG, insertbackground=FG,
                 relief=tk.FLAT, font=F_SM).pack(**P)

        # ── Sensors ───────────────────────────────────────────────────────
        self._sec(inner, "SENSORS")
        self._chk(inner, "ECG  (130 Hz, 14-bit μV)", self.opt_ecg).pack(**P)
        self._chk(inner, "Accelerometer",             self.opt_acc).pack(**P)

        rf = tk.Frame(inner, bg=BG_PANEL)
        rf.pack(**P)
        tk.Label(rf, text="  Rate:", bg=BG_PANEL, fg=FG_DIM,
                 font=F_XS, width=7, anchor="w").pack(side=tk.LEFT)
        ttk.Combobox(rf, textvariable=self.opt_acc_rate,
                     values=["25", "50", "100", "200"],
                     width=6, state="readonly",
                     font=F_XS).pack(side=tk.LEFT)
        tk.Label(rf, text=" Hz", bg=BG_PANEL, fg=FG_DIM, font=F_XS).pack(side=tk.LEFT)

        rf2 = tk.Frame(inner, bg=BG_PANEL)
        rf2.pack(**P)
        tk.Label(rf2, text="  Range:", bg=BG_PANEL, fg=FG_DIM,
                 font=F_XS, width=7, anchor="w").pack(side=tk.LEFT)
        ttk.Combobox(rf2, textvariable=self.opt_acc_range,
                     values=["2", "4", "8"],
                     width=6, state="readonly",
                     font=F_XS).pack(side=tk.LEFT)
        tk.Label(rf2, text=" G", bg=BG_PANEL, fg=FG_DIM, font=F_XS).pack(side=tk.LEFT)

        self._chk(inner, "Battery level", self.opt_battery).pack(**P)
        self._chk(inner, "Device info",   self.opt_device_info).pack(**P)
        self._chk(inner, "Body location", self.opt_body_loc).pack(**P)

        # ── Breathing ──────────────────────────────────────────────────────
        self._sec(inner, "BREATHING")
        self._chk(inner, "Detect breathing", self.opt_resp).pack(**P)
        mf = tk.Frame(inner, bg=BG_PANEL)
        mf.pack(**P)
        tk.Label(mf, text="  Method:", bg=BG_PANEL, fg=FG_DIM,
                 font=F_XS, width=7, anchor="w").pack(side=tk.LEFT)
        ttk.Combobox(mf, textvariable=self.opt_resp_method,
                     values=["auto", "acc", "rsa"],
                     width=6, state="readonly",
                     font=F_XS).pack(side=tk.LEFT)
        tk.Label(inner, text="  acc = chest motion · rsa = HR variation",
                 bg=BG_PANEL, fg=FG_DIM, font=("Helvetica", 11), anchor="w").pack(**P)

        # ── Behaviour ─────────────────────────────────────────────────────
        self._sec(inner, "BEHAVIOUR")
        self._chk(inner, "Auto-reconnect",         self.opt_reconnect).pack(**P)
        self._chk(inner, "Reset energy on connect", self.opt_reset_energy).pack(**P)
        self._chk(inner, "Verbose logging",         self.opt_verbose).pack(**P)

        # ── ECG display ───────────────────────────────────────────────────
        self._sec(inner, "ECG DISPLAY")
        rf3 = tk.Frame(inner, bg=BG_PANEL)
        rf3.pack(**P)
        tk.Label(rf3, text="Window:", bg=BG_PANEL, fg=FG_DIM, font=F_XS).pack(side=tk.LEFT)
        tk.Spinbox(rf3, textvariable=self.opt_ecg_win, from_=1, to=60, increment=1,
                   width=4, bg=BG_ENTRY, fg=FG, insertbackground=FG,
                   buttonbackground=BG_PANEL, relief=tk.FLAT,
                   font=F_SM).pack(side=tk.LEFT, padx=4)
        tk.Label(rf3, text="s", bg=BG_PANEL, fg=FG_DIM, font=F_XS).pack(side=tk.LEFT)

        # ── ECG filter ────────────────────────────────────────────────────
        self._sec(inner, "ECG FILTER")
        self._chk(inner, "High-pass  0.5 Hz",  self.opt_filter_hp).pack(**P)
        self._chk(inner, "Low-pass   40 Hz",   self.opt_filter_lp).pack(**P)
        self._chk(inner, "Notch",               self.opt_filter_notch).pack(**P)
        nf = tk.Frame(inner, bg=BG_PANEL)
        nf.pack(**P)
        tk.Label(nf, text="  Freq:", bg=BG_PANEL, fg=FG_DIM,
                 font=F_XS, width=7, anchor="w").pack(side=tk.LEFT)
        ttk.Combobox(nf, textvariable=self.opt_notch_freq,
                     values=["50", "60"], width=6, state="readonly",
                     font=F_XS).pack(side=tk.LEFT)
        tk.Label(nf, text=" Hz", bg=BG_PANEL, fg=FG_DIM, font=F_XS).pack(side=tk.LEFT)
        self.opt_notch_freq.trace_add("write", lambda *_: self._update_notch())

        # ── Log ───────────────────────────────────────────────────────────
        self._sec(inner, "LOG")
        self._chk(inner, "Write log to file  (./log/)",
                  self.opt_log_file).pack(**P)
        self.log_text = tk.Text(
            inner, height=12, bg=BG_ENTRY, fg=FG_DIM,
            font=F_MONO, relief=tk.FLAT,
            state=tk.DISABLED, wrap=tk.WORD,
            selectbackground="#2a2a3e",
        )
        self.log_text.pack(fill=tk.X, padx=8, pady=2)
        tk.Button(inner, text="Clear log", command=self._clear_log,
                  bg=BG_ENTRY, fg=FG_DIM, relief=tk.FLAT,
                  cursor="hand2", font=F_XS).pack(fill=tk.X, padx=8, pady=(1, 12))

        return outer

    def _build_plots(self, parent: tk.Widget) -> tk.Widget:
        frame = tk.Frame(parent, bg=BG_ROOT)

        # ── Large numeric readouts strip ──────────────────────────────────
        readouts = tk.Frame(frame, bg=BG_PANEL, height=120)
        readouts.pack(side=tk.TOP, fill=tk.X, pady=(0, 3))
        readouts.pack_propagate(False)

        hr_box = tk.Frame(readouts, bg=BG_PANEL)
        hr_box.pack(side=tk.LEFT, padx=(28, 8), pady=8)
        tk.Label(hr_box, textvariable=self.sv_hr_num,
                 bg=BG_PANEL, fg=RED,
                 font=("Helvetica", 66, "bold")).pack()
        tk.Label(hr_box, text="BPM  ·  HEART RATE",
                 bg=BG_PANEL, fg=FG_DIM, font=F_SM).pack()

        tk.Frame(readouts, bg="#2a2a3e", width=2).pack(
            side=tk.LEFT, fill=tk.Y, padx=20, pady=12)

        hrv_box = tk.Frame(readouts, bg=BG_PANEL)
        hrv_box.pack(side=tk.LEFT, padx=(8, 8), pady=8)
        tk.Label(hrv_box, textvariable=self.sv_hrv_num,
                 bg=BG_PANEL, fg=ORANGE,
                 font=("Helvetica", 66, "bold")).pack()
        tk.Label(hrv_box, text="ms  ·  HRV (RMSSD)",
                 bg=BG_PANEL, fg=FG_DIM, font=F_SM).pack()

        tk.Frame(readouts, bg="#2a2a3e", width=2).pack(
            side=tk.LEFT, fill=tk.Y, padx=20, pady=12)

        br_box = tk.Frame(readouts, bg=BG_PANEL)
        br_box.pack(side=tk.LEFT, padx=(8, 8), pady=8)
        tk.Label(br_box, textvariable=self.sv_br_num,
                 bg=BG_PANEL, fg=CYAN,
                 font=("Helvetica", 66, "bold")).pack()
        tk.Label(br_box, text="br/min  ·  BREATHING",
                 bg=BG_PANEL, fg=FG_DIM, font=F_SM).pack()

        # Vertical inhale/exhale indicator (top = max inhale, bottom = max exhale)
        breath_bar_box = tk.Frame(readouts, bg=BG_PANEL)
        breath_bar_box.pack(side=tk.LEFT, padx=(8, 8), pady=8)
        self.canvas_breath = tk.Canvas(breath_bar_box, width=34, height=104,
                                       bg=BG_PANEL, highlightthickness=0, bd=0)
        self.canvas_breath.pack()
        self._build_breath_bar()

        # Breathing signal-strength readouts: quality (0–1) + amplitude (chest motion).
        # Lets the user see whether the sensor is picking up enough signal to trust the
        # breathing estimate before recording. Both values are colour-coded.
        sig_box = tk.Frame(readouts, bg=BG_PANEL)
        sig_box.pack(side=tk.LEFT, padx=(16, 8), pady=8)
        tk.Label(sig_box, text="SIGNAL", bg=BG_PANEL, fg=FG_DIM,
                 font=F_SM).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 2))
        self.lbl_quality = tk.Label(sig_box, textvariable=self.sv_quality,
                                    bg=BG_PANEL, fg=FG_DIM, font=("Helvetica", 26, "bold"))
        self.lbl_quality.grid(row=1, column=0, sticky="e")
        tk.Label(sig_box, text="quality", bg=BG_PANEL, fg=FG_DIM,
                 font=F_XS).grid(row=1, column=1, sticky="w", padx=(6, 0))
        self.lbl_amp = tk.Label(sig_box, textvariable=self.sv_amp,
                               bg=BG_PANEL, fg=FG_DIM, font=("Helvetica", 26, "bold"))
        self.lbl_amp.grid(row=2, column=0, sticky="e")
        self.lbl_amp_unit = tk.Label(sig_box, text="motion", bg=BG_PANEL, fg=FG_DIM,
                                    font=F_XS)
        self.lbl_amp_unit.grid(row=2, column=1, sticky="w", padx=(6, 0))


        # ── ECG ───────────────────────────────────────────────────────────
        ecg_frame = tk.Frame(frame, bg=BG_ROOT)
        ecg_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.fig_ecg = Figure()
        self.ax_ecg  = self.fig_ecg.add_subplot(111)
        self._style_ecg_ax(self.ax_ecg)
        self.ax_ecg.set_xlim(-ECG_WIN_DEF, 0.2)
        self.ax_ecg.set_ylim(-1.5, 1.5)
        # two-layer glow: wide soft outer halo + bright inner trace
        self.line_ecg_glow, = self.ax_ecg.plot([], [], lw=6.0, color=CYAN,
                                                alpha=0.15, antialiased=True, zorder=2)
        self.line_ecg,      = self.ax_ecg.plot([], [], lw=1.2, color="#c8fbff",
                                                antialiased=True, zorder=3)
        self.canvas_ecg = FigureCanvasTkAgg(self.fig_ecg, master=ecg_frame)
        self.canvas_ecg.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # ── Bottom row: HR | HRV | ACC | Breathing ────────────────────────
        bottom = tk.Frame(frame, bg=BG_ROOT, height=260)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        bottom.pack_propagate(False)

        hr_frame  = tk.Frame(bottom, bg=BG_ROOT)
        hr_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 2))
        hrv_frame = tk.Frame(bottom, bg=BG_ROOT)
        hrv_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 2))
        acc_frame = tk.Frame(bottom, bg=BG_ROOT)
        acc_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 2))
        resp_frame = tk.Frame(bottom, bg=BG_ROOT)
        resp_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.fig_hr = Figure()
        self.ax_hr  = self.fig_hr.add_subplot(111)
        self._style_ax(self.ax_hr, "Heart Rate", "seconds", "BPM")
        self.ax_hr.set_ylim(40, 200)
        self.line_hr, = self.ax_hr.plot([], [], lw=1.5, color=RED,
                                         marker="o", ms=3,
                                         markerfacecolor=MAGENTA,
                                         markeredgewidth=0)
        self.canvas_hr = FigureCanvasTkAgg(self.fig_hr, master=hr_frame)
        self.canvas_hr.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        self.fig_hrv = Figure()
        self.ax_hrv  = self.fig_hrv.add_subplot(111)
        self._style_ax(self.ax_hrv, "HRV (RMSSD)", "seconds", "ms")
        self.ax_hrv.set_ylim(0, 100)
        self.line_hrv, = self.ax_hrv.plot([], [], lw=1.5, color=ORANGE,
                                           marker="o", ms=3,
                                           markerfacecolor=YELLOW,
                                           markeredgewidth=0)
        self.canvas_hrv = FigureCanvasTkAgg(self.fig_hrv, master=hrv_frame)
        self.canvas_hrv.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        self.fig_acc = Figure()
        self.ax_acc  = self.fig_acc.add_subplot(111)
        self._style_ax(self.ax_acc, "Accelerometer", "seconds", "milliG")
        self.ax_acc.set_ylim(-2000, 2000)
        self.line_ax, = self.ax_acc.plot([], [], lw=0.9, color=RED,   label="X")
        self.line_ay, = self.ax_acc.plot([], [], lw=0.9, color=GREEN, label="Y")
        self.line_az, = self.ax_acc.plot([], [], lw=0.9, color=BLUE,  label="Z")
        self.ax_acc.legend(loc="upper right", fontsize=10, framealpha=0.6)
        self.canvas_acc = FigureCanvasTkAgg(self.fig_acc, master=acc_frame)
        self.canvas_acc.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Breathing waveform (derived from ACC or RSA in heart_rate_mon.py)
        self.fig_resp = Figure()
        self.ax_resp  = self.fig_resp.add_subplot(111)
        self._style_ax(self.ax_resp, "Breathing", "seconds", "wave")
        self.ax_resp.set_ylim(-1.2, 1.2)
        self.ax_resp.axhline(0.0, color="#2a2a3e", lw=0.6, zorder=1)
        self.line_resp_glow, = self.ax_resp.plot([], [], lw=5.0, color=CYAN,
                                                 alpha=0.15, antialiased=True, zorder=2)
        self.line_resp,      = self.ax_resp.plot([], [], lw=1.6, color=CYAN,
                                                 antialiased=True, zorder=3)
        self.canvas_resp = FigureCanvasTkAgg(self.fig_resp, master=resp_frame)
        self.canvas_resp.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        return frame

    def _build_statusbar(self) -> None:
        bar = tk.Frame(self.root, bg=BG_PANEL, height=36)
        bar.pack(side=tk.BOTTOM, fill=tk.X, padx=4, pady=(0, 4))
        bar.pack_propagate(False)

        for label, var, color in (
            ("Device:",  self.sv_device,  FG),
            ("HR:",      self.sv_hr,      RED),
            ("RR:",      self.sv_rr,      ORANGE),
            ("Battery:", self.sv_battery, GREEN),
            ("Contact:", self.sv_contact, YELLOW),
            ("ACC mg:",  self.sv_acc,     CYAN),
        ):
            tk.Label(bar, text=label, bg=BG_PANEL, fg=FG_DIM,
                     font=F_MD).pack(side=tk.LEFT, padx=(10, 2))
            tk.Label(bar, textvariable=var, bg=BG_PANEL, fg=color,
                     font=("Helvetica", 15, "bold")).pack(side=tk.LEFT, padx=(0, 8))

    # ── widget helpers ────────────────────────────────────────────────────────
    def _chk(self, parent, text, var) -> tk.Checkbutton:
        return tk.Checkbutton(
            parent, text=text, variable=var,
            bg=BG_PANEL, fg=FG, selectcolor=BG_ENTRY,
            activebackground=BG_PANEL, activeforeground=FG,
            font=F_SM,
        )

    def _sec(self, parent, title: str) -> None:
        f = tk.Frame(parent, bg=BG_PANEL)
        f.pack(fill=tk.X, padx=8, pady=(12, 3))
        tk.Label(f, text=title, bg=BG_PANEL, fg=CYAN, font=F_SEC).pack(side=tk.LEFT)
        rule = tk.Canvas(f, bg=BG_PANEL, height=1, highlightthickness=0, bd=0)
        rule.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0), pady=6)
        rule.bind("<Configure>",
                  lambda e, c=rule: c.create_line(0, 0, e.width, 0, fill=CYAN))

    def _style_ax(self, ax, title: str, xlabel: str, ylabel: str) -> None:
        ax.set_title(title, fontsize=12, pad=4, fontweight="bold")
        ax.set_xlabel(xlabel, fontsize=10)
        ax.set_ylabel(ylabel, fontsize=10)
        ax.tick_params(labelsize=9, length=3, which="both")
        ax.minorticks_on()
        ax.grid(True, which="major", color="#1e1e30", linewidth=0.7, linestyle="-")
        ax.grid(True, which="minor", color="#141420", linewidth=0.35, linestyle="-")
        for sp in ax.spines.values():
            sp.set_linewidth(0.5)

    def _style_ecg_ax(self, ax) -> None:
        """ECG axis styled like standard clinical paper (0.2 s / 0.5 mV major grid)."""
        ax.set_title("ECG", fontsize=12, pad=4, fontweight="bold")
        ax.set_xlabel("seconds", fontsize=10)
        ax.set_ylabel("mV", fontsize=10)
        for sp in ax.spines.values():
            sp.set_linewidth(0.5)
        # Grid lines: major every 0.2 s / 0.5 mV, minor every 0.04 s / 0.1 mV
        ax.xaxis.set_major_locator(MultipleLocator(0.2))
        ax.xaxis.set_minor_locator(MultipleLocator(0.04))
        ax.yaxis.set_major_locator(MultipleLocator(0.5))
        ax.yaxis.set_minor_locator(MultipleLocator(0.1))
        # Labels only at whole-second marks on x-axis; suppress all minor-tick labels
        ax.xaxis.set_major_formatter(
            FuncFormatter(lambda x, _: f"{x:.0f}" if abs(x - round(x)) < 0.05 else ""))
        ax.xaxis.set_minor_formatter(NullFormatter())
        ax.yaxis.set_minor_formatter(NullFormatter())
        ax.tick_params(axis="x", which="major", labelsize=9, length=4)
        ax.tick_params(axis="x", which="minor", length=2, labelbottom=False)
        ax.tick_params(axis="y", which="major", labelsize=9, length=4)
        ax.tick_params(axis="y", which="minor", length=2, labelleft=False)
        ax.grid(True, which="major", color="#1c4040", linewidth=0.8, linestyle="-", zorder=1)
        ax.grid(True, which="minor", color="#0c2424", linewidth=0.35, linestyle="-", zorder=1)

    # ── initial draw ──────────────────────────────────────────────────────────
    def _initial_draw(self) -> None:
        for canvas in (self.canvas_ecg, self.canvas_hr,
                       self.canvas_hrv, self.canvas_acc, self.canvas_resp):
            try:
                canvas.draw()
            except Exception as exc:
                self._log(f"[initial draw] {exc}")

    # ── ECG filter design ─────────────────────────────────────────────────────
    def _build_filters(self) -> None:
        fs = ECG_RATE
        try:
            self.sos_hp = butter(4, 0.5,  btype="high", fs=fs, output="sos")
            self.sos_lp = butter(4, 40.0, btype="low",  fs=fs, output="sos")
        except Exception:
            self.sos_hp = self.sos_lp = None
        self._update_notch()

    def _update_notch(self) -> None:
        try:
            freq = float(self.opt_notch_freq.get())
            b, a = iirnotch(freq, Q=30, fs=ECG_RATE)
            self.sos_notch = tf2sos(b, a)
        except Exception:
            self.sos_notch = None

    # ── connection ────────────────────────────────────────────────────────────
    def _build_cmd(self) -> list:
        cmd = [PYTHON, MONITOR_SCRIPT]
        device = self.opt_device.get().strip()
        if device:
            cmd += ["--device", device]
        if self.opt_ecg.get():
            cmd.append("--ecg")
        if self.opt_acc.get():
            cmd += ["--acc",
                    "--acc-rate",  self.opt_acc_rate.get(),
                    "--acc-range", self.opt_acc_range.get()]
        if self.opt_battery.get():
            cmd.append("--battery")
        if self.opt_device_info.get():
            cmd.append("--device-info")
        if self.opt_body_loc.get():
            cmd.append("--body-location")
        if self.opt_resp.get():
            cmd += ["--resp", "--resp-method", self.opt_resp_method.get()]
        if self.opt_reconnect.get():
            cmd.append("--reconnect")
        if self.opt_reset_energy.get():
            cmd.append("--reset-energy")
        if self.opt_verbose.get():
            cmd.append("--verbose")
        return cmd

    def _connect(self) -> None:
        if self.proc is not None:
            return
        if not os.path.exists(MONITOR_SCRIPT):
            messagebox.showerror("Script not found", MONITOR_SCRIPT)
            return
        cmd = self._build_cmd()
        self._log("$ " + " ".join(cmd))
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except Exception as exc:
            messagebox.showerror("Launch error", str(exc))
            return
        self.connect_time = time.time()
        self._clear_plots()
        self.sv_status.set("Connecting…")
        self.btn_connect.config(state=tk.DISABLED)
        self.btn_disconnect.config(state=tk.NORMAL)
        self.btn_pause.config(state=tk.NORMAL)
        if self.opt_log_file.get():
            self._open_log_file()
        threading.Thread(target=self._stdout_reader, daemon=True).start()
        threading.Thread(target=self._stderr_reader, daemon=True).start()

    def _stdout_reader(self) -> None:
        try:
            for line in self.proc.stdout:
                line = line.rstrip()
                if line:
                    self.data_queue.put(("data", line))
        except Exception:
            pass
        self.data_queue.put(("eof", None))

    def _stderr_reader(self) -> None:
        try:
            for line in self.proc.stderr:
                line = line.rstrip()
                if line:
                    self.data_queue.put(("log", line))
        except Exception:
            pass

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        if self._paused:
            self.btn_pause.config(text="▶  Continue")
        else:
            self.btn_pause.config(text="⏸  Pause")

    def _disconnect(self) -> None:
        proc, self.proc = self.proc, None
        if proc:
            def _kill():
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    pass
            threading.Thread(target=_kill, daemon=True).start()
        self._on_disconnected()

    def _open_log_file(self) -> None:
        log_dir = os.path.join(SCRIPT_DIR, "log")
        os.makedirs(log_dir, exist_ok=True)
        fname = time.strftime("%Y-%m-%d_%H-%M-%S") + ".log"
        path  = os.path.join(log_dir, fname)
        try:
            self._log_fh = open(path, "w", encoding="utf-8", buffering=1)
            self._log(f"Log → {path}")
        except OSError as exc:
            self._log(f"[log file] {exc}")
            self._log_fh = None

    def _close_log_file(self) -> None:
        if self._log_fh:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None

    def _on_disconnected(self) -> None:
        self.sv_status.set("Disconnected")
        self.btn_connect.config(state=tk.NORMAL)
        self.btn_disconnect.config(state=tk.DISABLED)
        self._paused = False
        self.btn_pause.config(text="⏸  Pause", state=tk.DISABLED)
        self.sv_device.set("—")
        self.sv_hr.set("—")
        self.sv_hr_num.set("—")
        self.sv_hrv_num.set("—")
        self.sv_br_num.set("—")
        self.sv_battery.set("—")
        self.sv_contact.set("—")
        self.sv_rr.set("—")
        self.sv_acc.set("—")
        self._resp_quality = None
        self._resp_amplitude = None
        self._update_signal_readout()
        self._draw_breath_bar(None)
        if self.capturing:
            self._stop_capture()
        self._log("─── disconnected ───")
        self._close_log_file()

    # ── recording ─────────────────────────────────────────────────────────────
    def _browse_capture(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".jsonl",
            filetypes=[("JSON Lines", "*.jsonl"),
                       ("JSON", "*.json"),
                       ("All files", "*.*")],
            title="Choose capture file",
        )
        if path:
            self._cappath = path
            self.sv_capfile.set(os.path.basename(path))

    def _toggle_capture(self) -> None:
        if self.capturing:
            self._stop_capture()
        else:
            self._start_capture()

    def _start_capture(self) -> None:
        if not self._cappath:
            messagebox.showwarning("No file",
                                   "Click Browse… to choose a capture file first.")
            return
        try:
            self.capture_file = open(self._cappath, "a", encoding="utf-8")
            self.capturing = True
            self.btn_record.config(text="⏹  Stop Recording",
                                   bg=BG_ENTRY, fg=RED, activebackground=BG_ENTRY)
            self._log(f"Recording → {self._cappath}")
        except OSError as exc:
            messagebox.showerror("File error", str(exc))

    def _stop_capture(self) -> None:
        self.capturing = False
        if self.capture_file:
            self.capture_file.close()
            self.capture_file = None
        self.btn_record.config(text="⏺  Record",
                               bg=BG_ENTRY, fg=FG, activebackground=BG_ENTRY)
        self._log("Recording stopped.")

    # ── log ───────────────────────────────────────────────────────────────────
    def _log(self, msg: str) -> None:
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        n = int(self.log_text.index("end-1c").split(".")[0])
        if n > 400:
            self.log_text.delete("1.0", f"{n - 300}.0")
        self.log_text.config(state=tk.DISABLED)
        if self._log_fh:
            try:
                self._log_fh.write(msg + "\n")
            except OSError:
                pass

    def _clear_log(self) -> None:
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    # ── poll loop ─────────────────────────────────────────────────────────────
    def _poll(self) -> None:
        self.root.after(POLL_MS, self._poll)  # reschedule first — exceptions can't kill the loop

        dirty_ecg = dirty_hr = dirty_acc = dirty_resp = False
        try:
            for _ in range(MAX_DRAIN):
                try:
                    kind, payload = self.data_queue.get_nowait()
                except queue.Empty:
                    break

                if kind == "eof":
                    if self.proc is not None:
                        self.proc = None
                        self._on_disconnected()
                    break

                if kind == "log":
                    self._log(payload)
                    continue

                if kind != "data":
                    continue

                # discard stale records that arrived after an explicit disconnect
                if self.proc is None:
                    continue

                if self.capturing and self.capture_file:
                    try:
                        self.capture_file.write(payload + "\n")
                        self.capture_file.flush()
                    except OSError:
                        pass

                try:
                    rec = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                self._pkt_count += 1
                rtype = rec.get("type")
                if rtype == "hr":
                    self._on_hr(rec);         dirty_hr   = True
                elif rtype == "ecg":
                    self._on_ecg(rec);        dirty_ecg  = True
                elif rtype == "acc":
                    self._on_acc(rec);        dirty_acc  = True
                elif rtype == "resp":
                    self._on_resp(rec);       dirty_resp = True
                elif rtype == "device_info":
                    self._on_device_info(rec)
                elif rtype == "body_location":
                    self._log(f"Body location: {rec.get('location', '?')}")

            if self._pkt_count:
                self.sv_pkts.set(f"{self._pkt_count} packets")

        except Exception as exc:
            self._log(f"[poll error] {exc}")

        try:
            if not self._paused:
                if dirty_ecg:  self._draw_ecg()
                if dirty_hr:   self._draw_hr(); self._draw_hrv()
                if dirty_acc:  self._draw_acc()
                if dirty_resp: self._draw_resp()
        except Exception as exc:
            self._log(f"[draw error] {exc}")

    # ── record handlers ───────────────────────────────────────────────────────
    def _elapsed(self) -> float:
        return time.time() - self.connect_time if self.connect_time else 0.0

    def _on_hr(self, rec: dict) -> None:
        self.sv_status.set("Connected")
        if rec.get("device"):
            self.sv_device.set(rec["device"])
        bpm = rec.get("heart_rate_bpm")
        if bpm is not None:
            self.sv_hr.set(f"{bpm} bpm")
            self.sv_hr_num.set(str(bpm))
            self.hr_t.append(self._elapsed())
            self.hr_v.append(bpm)
        if rec.get("battery_pct") is not None:
            self.sv_battery.set(f"{rec['battery_pct']}%")
        if rec.get("contact") is not None:
            self.sv_contact.set("Yes" if rec["contact"] else "No")
        rr = rec.get("rr_intervals_ms", [])
        if rr:
            self.sv_rr.set(f"{sum(rr)/len(rr):.0f} ms")
            self.rr_rolling.extend(rr)
            val = _rmssd(list(self.rr_rolling))
            if val is not None:
                self.hrv_t.append(self._elapsed())
                self.hrv_v.append(val)
                self.sv_hrv_num.set(f"{val:.0f}")

    def _on_ecg(self, rec: dict) -> None:
        samples = rec.get("samples_uv", [])
        if samples:
            self.ecg_buf.extend(samples)

    def _on_acc(self, rec: dict) -> None:
        samples = rec.get("samples_mg", [])
        if len(self.acc_t) == 0:          # one-time diagnostic on first ACC record
            rate = rec.get("sample_rate_hz", "?")
            self._log(f"ACC first frame: {len(samples)} sample(s)  rate={rate} Hz"
                      + ("" if samples else "  ← empty (check log for frame_type warning)"))
        if not samples:
            return
        t_now = self._elapsed()
        rate  = rec.get("sample_rate_hz") or 25
        dt    = 1.0 / rate
        n     = len(samples)
        for i, s in enumerate(samples):
            self.acc_t.append(t_now - (n - 1 - i) * dt)
            self.acc_x.append(s.get("x", 0.0))
            self.acc_y.append(s.get("y", 0.0))
            self.acc_z.append(s.get("z", 0.0))
        last = samples[-1]
        self.sv_acc.set(f"X:{last.get('x', 0):+.0f}  Y:{last.get('y', 0):+.0f}  Z:{last.get('z', 0):+.0f}")

    def _on_resp(self, rec: dict) -> None:
        wave = rec.get("waveform")
        if wave is not None:
            self.resp_t.append(self._elapsed())
            self.resp_v.append(wave)
            self._draw_breath_bar(wave)
        self._resp_method  = rec.get("method", "") or ""
        q = rec.get("quality")
        self._resp_quality = q if isinstance(q, (int, float)) else None
        amp = rec.get("amplitude")
        self._resp_amplitude = amp if isinstance(amp, (int, float)) else None
        rate = rec.get("breathing_rate_brpm")
        if rate is not None:
            self.sv_br_num.set(f"{rate:.1f}")
        self._update_signal_readout()

    def _update_signal_readout(self) -> None:
        """Refresh the colour-coded breathing quality + amplitude readouts."""
        q = self._resp_quality
        if q is None:
            self.sv_quality.set("—"); self.lbl_quality.config(fg=FG_DIM)
        else:
            self.sv_quality.set(f"{q:.2f}")
            self.lbl_quality.config(
                fg=GREEN if q >= 0.60 else YELLOW if q >= 0.35 else RED)

        amp = self._resp_amplitude
        is_rsa = self._resp_method == "rsa"
        self.lbl_amp_unit.config(text="bpm" if is_rsa else "mg")
        if amp is None:
            self.sv_amp.set("—"); self.lbl_amp.config(fg=FG_DIM)
        else:
            self.sv_amp.set(f"{amp:.0f}")
            # ACC motion thresholds (mg). Calibrated against real captures: clean
            # meditative breathing reads ~20 mg and scores quality ~0.9, while the
            # noise floor / non-breathing motion sits below ~10 mg. Amplitude only
            # tells you the strap is picking up motion; quality is the trust signal.
            # RSA HR-modulation thresholds (bpm): good ~3+, weak <1.
            good, marg = (3.0, 1.0) if is_rsa else (20.0, 10.0)
            self.lbl_amp.config(
                fg=GREEN if amp >= good else YELLOW if amp >= marg else RED)

    def _clear_plots(self) -> None:
        self.ecg_buf.clear()
        self.hr_t.clear();  self.hr_v.clear()
        self.hrv_t.clear(); self.hrv_v.clear()
        self.rr_rolling.clear()
        self.acc_t.clear(); self.acc_x.clear(); self.acc_y.clear(); self.acc_z.clear()
        self.resp_t.clear(); self.resp_v.clear()
        self._resp_method = ""; self._resp_quality = None; self._resp_amplitude = None
        self._update_signal_readout()
        self._draw_breath_bar(None)
        self._pkt_count = 0
        self.sv_pkts.set("")
        for line in (self.line_ecg_glow, self.line_ecg,
                     self.line_hr, self.line_hrv,
                     self.line_ax, self.line_ay, self.line_az,
                     self.line_resp, self.line_resp_glow):
            line.set_data([], [])
        for canvas in (self.canvas_ecg, self.canvas_hr,
                       self.canvas_hrv, self.canvas_acc, self.canvas_resp):
            canvas.draw()

    def _on_device_info(self, rec: dict) -> None:
        parts = [rec.get("device", ""),
                 rec.get("model_number", ""),
                 f"FW {rec['firmware_revision']}" if rec.get("firmware_revision") else "",
                 f"SN {rec['serial_number']}"     if rec.get("serial_number")     else ""]
        self._log("Device: " + "  |  ".join(p for p in parts if p))
        if rec.get("device"):
            self.sv_device.set(rec["device"])

    # ── breathing inhale/exhale bar ─────────────────────────────────────────────
    def _build_breath_bar(self) -> None:
        """Draw the static parts of the vertical inhale/exhale indicator (once)."""
        c = self.canvas_breath
        self._bar_x0, self._bar_x1 = 11, 23
        self._bar_top, self._bar_bot = 18, 86      # track extent (y px)
        y_mid = (self._bar_top + self._bar_bot) / 2
        # track + neutral midline
        c.create_rectangle(self._bar_x0, self._bar_top, self._bar_x1, self._bar_bot,
                           fill=BG_ENTRY, outline="#2a2a3e", width=1)
        c.create_line(self._bar_x0 - 2, y_mid, self._bar_x1 + 2, y_mid,
                     fill="#2a2a3e", width=1)
        c.create_text(17, 8,  text="IN", fill=FG_DIM, font=("Helvetica", 9, "bold"))
        c.create_text(17, 97, text="EX", fill=FG_DIM, font=("Helvetica", 9, "bold"))
        # dynamic items — created once, then moved via coords()
        self._bar_fill = c.create_rectangle(self._bar_x0, y_mid, self._bar_x1, self._bar_bot,
                                            fill=CYAN, outline="")
        self._bar_cap  = c.create_line(self._bar_x0 - 2, y_mid, self._bar_x1 + 2, y_mid,
                                      fill="#c8fbff", width=2)
        self._breath_drawn = True

    def _draw_breath_bar(self, w: Optional[float]) -> None:
        """Update the indicator. w = normalized waveform (-1..1); None resets to neutral."""
        if not getattr(self, "_breath_drawn", False):
            return
        w = 0.0 if w is None else max(-1.0, min(1.0, float(w)))
        f = (w + 1.0) / 2.0                       # 0 = max exhale, 1 = max inhale
        span = self._bar_bot - self._bar_top
        y_level = self._bar_bot - f * span        # inhale → toward top (smaller y)
        self.canvas_breath.coords(self._bar_fill,
                                  self._bar_x0, y_level, self._bar_x1, self._bar_bot)
        self.canvas_breath.coords(self._bar_cap,
                                  self._bar_x0 - 2, y_level, self._bar_x1 + 2, y_level)

    # ── plot redraws ──────────────────────────────────────────────────────────
    def _draw_ecg(self) -> None:
        n = len(self.ecg_buf)
        if n < 2:
            return
        raw = np.array(list(self.ecg_buf), dtype=float)
        # Zero-phase filtering (sosfiltfilt: no group-delay, preserves QRS peak shape).
        # The 0.5 Hz HP filter has a ~260-sample settling time at 130 Hz.  sosfiltfilt's
        # default padlen (~15) is far too short — the backward-pass transient bleeds into
        # the most-recent (displayed) samples, causing the axis to blow out.  We pad with
        # 6× the settling period so both forward and backward passes are fully settled
        # before they reach actual data.
        _HP_PAD = min(n - 1, int(6 * ECG_RATE))   # up to 780 samples (6 s)
        if n > 26:
            try:
                if self.opt_filter_hp.get() and self.sos_hp is not None:
                    raw = sosfiltfilt(self.sos_hp, raw, padlen=_HP_PAD)
                if self.opt_filter_lp.get() and self.sos_lp is not None:
                    raw = sosfiltfilt(self.sos_lp, raw)
                if self.opt_filter_notch.get() and self.sos_notch is not None:
                    raw = sosfiltfilt(self.sos_notch, raw)
            except Exception as exc:
                self._log(f"[ECG filter error] {exc}")
        mv = raw / 1000.0   # μV → mV (standard ECG units)
        window = max(1.0, min(60.0, self.opt_ecg_win.get()))
        target_maxlen = int(window * ECG_RATE * 3)
        if self.ecg_buf.maxlen != target_maxlen:
            self.ecg_buf = collections.deque(self.ecg_buf, maxlen=target_maxlen)
        x = [(i - n + 1) / ECG_RATE for i in range(n)]
        self.line_ecg_glow.set_data(x, mv)
        self.line_ecg.set_data(x, mv)
        self.ax_ecg.set_xlim(-window, 0.2)
        # Y-axis: centre on the median of the visible window; scale to the 99th-percentile
        # absolute deviation from that median so the QRS peak (only ~10 % of samples) is
        # captured without letting rare spikes dictate the range.
        n_vis  = min(n, int(window * ECG_RATE) + 1)
        mv_vis = mv[-n_vis:]
        med  = float(np.median(mv_vis))
        peak = float(np.percentile(np.abs(mv_vis - med), 99))
        half = float(min(max(1.0, peak * 1.3), 5.0))   # 30 % headroom, cap at ±5 mV
        self.ax_ecg.set_ylim(med - half, med + half)
        # Avoid crowding y-axis labels when range is large
        if half > 3.0:
            self.ax_ecg.yaxis.set_major_locator(MultipleLocator(1.0))
            self.ax_ecg.yaxis.set_minor_locator(MultipleLocator(0.2))
        else:
            self.ax_ecg.yaxis.set_major_locator(MultipleLocator(0.5))
            self.ax_ecg.yaxis.set_minor_locator(MultipleLocator(0.1))
        self.canvas_ecg.draw()

    def _draw_hr(self) -> None:
        if not self.hr_t:
            return
        t, v = list(self.hr_t), list(self.hr_v)
        self.line_hr.set_data(t, v)
        t_end = t[-1]
        self.ax_hr.set_xlim(max(0.0, t_end - HR_WIN_S), t_end + 2.0)
        lo, hi = min(v), max(v)
        pad = max(5, (hi - lo) * 0.2)
        self.ax_hr.set_ylim(max(0, lo - pad), hi + pad)
        self.canvas_hr.draw()

    def _draw_hrv(self) -> None:
        if not self.hrv_t:
            return
        t, v = list(self.hrv_t), list(self.hrv_v)
        self.line_hrv.set_data(t, v)
        t_end = t[-1]
        self.ax_hrv.set_xlim(max(0.0, t_end - HR_WIN_S), t_end + 2.0)
        lo, hi = min(v), max(v)
        pad = max(5, (hi - lo) * 0.2)
        self.ax_hrv.set_ylim(max(0, lo - pad), hi + pad)
        self.canvas_hrv.draw()

    def _draw_acc(self) -> None:
        if not self.acc_t:
            return
        t  = list(self.acc_t)
        ax = list(self.acc_x)
        ay = list(self.acc_y)
        az = list(self.acc_z)
        self.line_ax.set_data(t, ax)
        self.line_ay.set_data(t, ay)
        self.line_az.set_data(t, az)
        t_end = t[-1]
        self.ax_acc.set_xlim(max(0.0, t_end - ACC_WIN_S), t_end + 0.2)
        all_v = ax + ay + az
        # Clamp to ±8G (8000 mg per axis) so a single corrupted sample can't blow the axis.
        all_v = [v for v in all_v if abs(v) <= 8000]
        if not all_v:
            return
        lo, hi = min(all_v), max(all_v)
        pad = max(50, (hi - lo) * 0.1)
        self.ax_acc.set_ylim(lo - pad, hi + pad)
        self.canvas_acc.draw()

    def _draw_resp(self) -> None:
        if not self.resp_t:
            return
        t, v = list(self.resp_t), list(self.resp_v)
        self.line_resp.set_data(t, v)
        self.line_resp_glow.set_data(t, v)
        t_end = t[-1]
        self.ax_resp.set_xlim(max(0.0, t_end - RESP_WIN_S), t_end + 0.5)
        self.ax_resp.set_ylim(-1.2, 1.2)
        # Title carries the live method + signal-quality readout.
        title = "Breathing"
        extra = []
        if self._resp_method:
            extra.append(self._resp_method)
        if self._resp_quality is not None:
            extra.append(f"q={self._resp_quality:.2f}")
        if extra:
            title += "  ·  " + "  ·  ".join(extra)
        self.ax_resp.set_title(title, fontsize=12, pad=4, fontweight="bold")
        self.canvas_resp.draw()



# ── app icon ──────────────────────────────────────────────────────────────────
def _ecg_waveform(t: np.ndarray, period: float = 1.0) -> np.ndarray:
    """Synthesise a single-lead PQRST ECG signal over array t."""
    y = np.zeros_like(t)
    for center in np.arange(period * 0.45, t[-1] + period, period):
        tc = t - center
        y += 0.14  * np.exp(-(tc + 0.30 * period)**2 / (2 * (0.025 * period)**2))  # P
        y -= 0.09  * np.exp(-(tc + 0.09 * period)**2 / (2 * (0.018 * period)**2))  # Q
        y += 1.00  * np.exp(-tc**2               / (2 * (0.014 * period)**2))       # R
        y -= 0.28  * np.exp(-(tc - 0.07 * period)**2 / (2 * (0.018 * period)**2))  # S
        y += 0.22  * np.exp(-(tc - 0.27 * period)**2 / (2 * (0.055 * period)**2))  # T
    return y


def _make_app_icon(root: tk.Tk) -> tk.PhotoImage:
    """Render an ECG waveform icon and return a PhotoImage (keep reference alive)."""
    import io, base64
    import matplotlib.backends.backend_agg as agg
    from matplotlib.figure import Figure as MplFigure

    PX, DPI = 256, 96                   # exact output size in pixels
    fig = MplFigure(figsize=(PX / DPI, PX / DPI), dpi=DPI)
    fig.patch.set_facecolor("#0a0a14")

    # small margins so the trace has breathing room
    ax = fig.add_axes([0.05, 0.10, 0.90, 0.80])
    ax.set_facecolor("#0a0a14")
    ax.axis("off")

    # show ~2 beats; period chosen so the QRS spike reaches ~80% of height
    t = np.linspace(0, 2.6, 1300)
    y = _ecg_waveform(t, period=1.1)

    ax.set_xlim(t[0], t[-1])
    ax.set_ylim(-0.38, 1.22)

    # ECG paper grid — just the large-box lines (0.2 s, 0.25 amplitude units)
    for xg in np.arange(0, t[-1] + 0.01, 0.22):
        ax.axvline(xg, color="#163030", lw=0.7, zorder=1)
    for yg in np.arange(-0.5, 1.3, 0.25):
        ax.axhline(yg, color="#163030", lw=0.7, zorder=1)

    # Glow trace (three layers)
    ax.plot(t, y, color="#00e5ff", lw=9,  alpha=0.09, solid_capstyle="round", zorder=2)
    ax.plot(t, y, color="#00e5ff", lw=3.5, alpha=0.32, solid_capstyle="round", zorder=3)
    ax.plot(t, y, color="#d4fcff", lw=1.3, solid_capstyle="round", zorder=4)

    canvas = agg.FigureCanvasAgg(fig)
    canvas.draw()
    buf = io.BytesIO()
    # No bbox_inches — keeps exact PX×PX output size
    fig.savefig(buf, format="png", dpi=DPI, facecolor="#0a0a14")
    import matplotlib.pyplot as _plt; _plt.close(fig)
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode()
    return tk.PhotoImage(data=b64)


# ── entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    root = tk.Tk()
    # Set app icon (used in dock when minimized / in mission control)
    try:
        _icon = _make_app_icon(root)   # must stay referenced — GC would destroy it
        root.iconphoto(True, _icon)
    except Exception:
        pass
    app  = MonitorApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (_close(app, root)))
    root.mainloop()


def _close(app: MonitorApp, root: tk.Tk) -> None:
    app._disconnect()
    root.destroy()


if __name__ == "__main__":
    main()
