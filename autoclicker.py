"""
AutoClicker Pro - A full-featured desktop auto clicker
Built with Tkinter (UI) + pynput (mouse/keyboard control)

Threading & Stopping Logic:
- A threading.Event is the authoritative stop signal and provides
  interruptible waits for both click intervals and held mouse buttons.
- Each worker has a run ID so callbacks from an older run cannot affect a
  newer run.
- The global hotkey listener runs in its own daemon thread (pynput's default).
- All requests from background threads go through a queue; Tkinter widgets
  are only accessed from the main thread.
"""

import tkinter as tk
from tkinter import ttk
from dataclasses import dataclass
import threading
import time
import queue
from pynput.mouse import Button, Controller as MouseController
from pynput.keyboard import Key, Listener as KeyListener


# ─────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────
APP_TITLE = "AutoClicker Pro"
VERSION   = "v1.1"
DEFAULT_HOTKEY = Key.f6
HOTKEY_DEBOUNCE_SEC = 0.3   # ignore repeated key events within this window
START_DELAY_SEC = 0.3

# Dark-theme palette
BG_DARK    = "#1a1a2e"
BG_PANEL   = "#16213e"
BG_WIDGET  = "#0f3460"
ACCENT     = "#e94560"
ACCENT2    = "#533483"
FG_PRIMARY = "#eaeaea"
FG_DIM     = "#8892a4"
FG_GREEN   = "#00ff9f"
FG_RED     = "#ff4d6d"
FONT_MAIN  = ("Consolas", 10)
FONT_BOLD  = ("Consolas", 10, "bold")
FONT_TITLE = ("Consolas", 14, "bold")
FONT_MONO  = ("Courier New", 9)


# ─────────────────────────────────────────────
#  Utility helpers
# ─────────────────────────────────────────────
def hms_to_seconds(h, m, s, ms=0):
    """Convert hours/minutes/seconds/milliseconds → total seconds (float)."""
    return h * 3600 + m * 60 + s + ms / 1000.0


def seconds_to_hms(total):
    """Convert total seconds → (h, m, s)."""
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = int(total % 60)
    return h, m, s


class ConfigError(ValueError):
    """Raised when a UI value cannot be converted into a safe click config."""


def _parse_int(value, label, minimum=None, maximum=None):
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be a whole number.") from exc
    if minimum is not None and parsed < minimum:
        raise ConfigError(f"{label} must be at least {minimum}.")
    if maximum is not None and parsed > maximum:
        raise ConfigError(f"{label} must be at most {maximum}.")
    return parsed


@dataclass(frozen=True)
class ClickConfig:
    """Validated, immutable settings consumed by a ClickWorker."""

    interval_seconds: float
    button: str
    click_type: str
    hold_seconds: float
    position_mode: str
    pos_x: int
    pos_y: int
    repeat_mode: str
    repeat_count: int
    duration_seconds: float

    @classmethod
    def from_values(cls, values):
        int_h = _parse_int(values["int_h"], "Interval hours", 0, 23)
        int_m = _parse_int(values["int_m"], "Interval minutes", 0, 59)
        int_s = _parse_int(values["int_s"], "Interval seconds", 0, 59)
        int_ms = _parse_int(values["int_ms"], "Interval milliseconds", 0, 999)
        interval = hms_to_seconds(int_h, int_m, int_s, int_ms)
        if interval < 0.01:
            raise ConfigError("Click interval must be at least 10 milliseconds.")

        button = values["button"]
        if button not in {"Left", "Right", "Middle"}:
            raise ConfigError("Choose a valid mouse button.")

        click_type = values["click_type"]
        if click_type not in {"Single", "Double", "Hold"}:
            raise ConfigError("Choose a valid click type.")
        hold_ms = 500
        if click_type == "Hold":
            hold_ms = _parse_int(
                values["hold_ms"], "Hold duration", 1, 10_000)

        position_mode = values["position_mode"]
        if position_mode not in {"current", "fixed"}:
            raise ConfigError("Choose a valid click position mode.")
        pos_x = 0
        pos_y = 0
        if position_mode == "fixed":
            pos_x = _parse_int(values["pos_x"], "X coordinate")
            pos_y = _parse_int(values["pos_y"], "Y coordinate")

        repeat_mode = values["repeat_mode"]
        if repeat_mode not in {"infinite", "count", "duration"}:
            raise ConfigError("Choose a valid repeat mode.")

        repeat_count = 1
        if repeat_mode == "count":
            repeat_count = _parse_int(
                values["repeat_count"], "Repeat count", 1, 9_999_999)

        duration = 0.0
        if repeat_mode == "duration":
            dur_h = _parse_int(values["dur_h"], "Duration hours", 0, 23)
            dur_m = _parse_int(values["dur_m"], "Duration minutes", 0, 59)
            dur_s = _parse_int(values["dur_s"], "Duration seconds", 0, 59)
            duration = hms_to_seconds(dur_h, dur_m, dur_s)
            if duration <= 0:
                raise ConfigError("Run duration must be greater than zero.")

        return cls(
            interval_seconds=interval,
            button=button,
            click_type=click_type,
            hold_seconds=hold_ms / 1000.0,
            position_mode=position_mode,
            pos_x=pos_x,
            pos_y=pos_y,
            repeat_mode=repeat_mode,
            repeat_count=repeat_count,
            duration_seconds=duration,
        )


# ─────────────────────────────────────────────
#  Click Worker
# ─────────────────────────────────────────────
class ClickWorker(threading.Thread):
    """Background thread that performs mouse actions for one run."""

    def __init__(self, config, run_id, on_click_cb, on_done_cb,
                 controller_factory=MouseController, start_delay=START_DELAY_SEC,
                 clock=time.monotonic):
        """
        Callbacks receive this worker's run ID so stale events can be ignored.
        controller_factory and clock are injectable for deterministic tests.
        """
        super().__init__(daemon=True)
        self.config      = config
        self.run_id      = run_id
        self.on_click_cb = on_click_cb
        self.on_done_cb  = on_done_cb
        self.controller_factory = controller_factory
        self.start_delay = start_delay
        self.clock = clock
        self.stop_event  = threading.Event()
        self._click_count = 0
        self._stop_reason = "Stopped"

    def stop(self, reason="Stopped"):
        """Signal the worker to stop and wake any interval or hold wait."""
        self._stop_reason = reason
        self.stop_event.set()

    def _perform_action(self, controller, button, deadline):
        """Perform one complete action; return False if a hold was interrupted."""
        cfg = self.config
        if cfg.position_mode == "fixed":
            controller.position = (cfg.pos_x, cfg.pos_y)

        if cfg.click_type == "Single":
            controller.click(button, 1)
            return True
        if cfg.click_type == "Double":
            controller.click(button, 2)
            return True

        hold_time = cfg.hold_seconds
        if deadline is not None:
            hold_time = min(hold_time, max(0.0, deadline - self.clock()))

        pressed = False
        try:
            controller.press(button)
            pressed = True
            interrupted = self.stop_event.wait(hold_time)
            if interrupted:
                return False
            return hold_time >= cfg.hold_seconds
        finally:
            if pressed:
                controller.release(button)

    # ── main loop ────────────────────────────
    def run(self):
        reason = "Stopped"
        cfg = self.config
        try:
            controller = self.controller_factory()
            button = {
                "Left": Button.left,
                "Right": Button.right,
                "Middle": Button.middle,
            }[cfg.button]

            # A short, fixed grace period lets the user move away from Start.
            if self.stop_event.wait(self.start_delay):
                return

            deadline = None
            if cfg.repeat_mode == "duration":
                deadline = self.clock() + cfg.duration_seconds

            while not self.stop_event.is_set():
                if deadline is not None and self.clock() >= deadline:
                    reason = "Duration elapsed"
                    break

                completed = self._perform_action(controller, button, deadline)
                if not completed:
                    if deadline is not None and not self.stop_event.is_set():
                        reason = "Duration elapsed"
                    break

                self._click_count += 1
                self.on_click_cb(self.run_id, self._click_count)

                if (cfg.repeat_mode == "count"
                        and self._click_count >= cfg.repeat_count):
                    reason = "Reached click limit"
                    break

                wait_time = cfg.interval_seconds
                if deadline is not None:
                    remaining = max(0.0, deadline - self.clock())
                    if remaining <= 0:
                        reason = "Duration elapsed"
                        break
                    wait_time = min(wait_time, remaining)

                if self.stop_event.wait(wait_time):
                    break
        except Exception as exc:
            reason = f"Error: {exc}"
        finally:
            if reason == "Stopped" and self.stop_event.is_set():
                reason = self._stop_reason
            self.on_done_cb(self.run_id, reason)


# ─────────────────────────────────────────────
#  Main Application
# ─────────────────────────────────────────────
class AutoClickerApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_TITLE} {VERSION}")
        self.root.resizable(False, False)
        self.root.configure(bg=BG_DARK)

        # ── state ──────────────────────────────
        self.worker: ClickWorker | None = None
        self.run_state = "stopped"
        self.active_run_id = 0
        self.total_clicks = 0
        self.ui_queue: queue.Queue = queue.Queue()
        self._closing = False
        self._shutdown_deadline = None
        self.hotkey_listener = None

        # hotkey debounce
        self._last_hotkey_time = 0.0
        self._hotkey_pressed   = False   # track key-down state

        # capture mode
        self._capture_after_id = None

        # ── build UI ───────────────────────────
        self._build_ui()
        self._apply_styles()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── start hotkey listener ──────────────
        self._start_hotkey_listener()

        # ── poll UI queue ──────────────────────
        self._poll_queue()

        # Center window
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"+{(sw-w)//2}+{(sh-h)//2}")

        self.log("AutoClicker Pro ready. Press F6 or click Start.")

    # ══════════════════════════════════════════
    #  UI CONSTRUCTION
    # ══════════════════════════════════════════
    def _build_ui(self):
        root = self.root

        # ── Title bar area ─────────────────────
        title_frame = tk.Frame(root, bg=BG_DARK, pady=10)
        title_frame.pack(fill="x", padx=16)
        tk.Label(title_frame, text="⚡ AutoClicker Pro",
                 font=FONT_TITLE, bg=BG_DARK, fg=ACCENT).pack(side="left")
        tk.Label(title_frame, text=VERSION,
                 font=FONT_MAIN, bg=BG_DARK, fg=FG_DIM).pack(side="left", padx=6)

        # ── Status bar ─────────────────────────
        status_frame = tk.Frame(root, bg=BG_PANEL, pady=8, padx=16)
        status_frame.pack(fill="x")

        self.status_dot = tk.Label(status_frame, text="●", font=("Consolas", 14),
                                   bg=BG_PANEL, fg=FG_RED)
        self.status_dot.pack(side="left")

        self.status_label = tk.Label(status_frame, text=" STOPPED",
                                     font=FONT_BOLD, bg=BG_PANEL, fg=FG_RED)
        self.status_label.pack(side="left")

        self.click_count_label = tk.Label(status_frame, text="Clicks: 0",
                                          font=FONT_BOLD, bg=BG_PANEL, fg=FG_DIM)
        self.click_count_label.pack(side="right")

        tk.Label(status_frame, text=f"Hotkey: F6",
                 font=FONT_MAIN, bg=BG_PANEL, fg=FG_DIM).pack(side="right", padx=12)

        # ── Main content ───────────────────────
        content = tk.Frame(root, bg=BG_DARK)
        content.pack(fill="both", padx=16, pady=8)

        left  = tk.Frame(content, bg=BG_DARK)
        right = tk.Frame(content, bg=BG_DARK)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right.pack(side="right", fill="both", expand=True)

        # ── Click Interval ─────────────────────
        self._section(left, "⏱  Click Interval")
        interval_frame = self._panel(left)
        labels = ["Hours", "Minutes", "Seconds", "Milliseconds"]
        defaults = [0, 0, 0, 100]
        self.int_vars = []
        for i, (lbl, default) in enumerate(zip(labels, defaults)):
            col = tk.Frame(interval_frame, bg=BG_PANEL)
            col.pack(side="left", padx=6, pady=6)
            tk.Label(col, text=lbl, font=("Consolas", 8), bg=BG_PANEL,
                     fg=FG_DIM).pack()
            var = tk.StringVar(value=str(default))
            self.int_vars.append(var)
            sb = tk.Spinbox(col, from_=0,
                            to=(23 if i == 0 else 59 if i < 3 else 999),
                            width=5, textvariable=var,
                            font=FONT_BOLD, bg=BG_WIDGET, fg=FG_PRIMARY,
                            insertbackground=FG_PRIMARY,
                            disabledbackground=BG_WIDGET,
                            disabledforeground=FG_PRIMARY,
                            buttonbackground=BG_WIDGET,
                            relief="flat", bd=0, highlightthickness=1,
                            highlightcolor=ACCENT2,
                            highlightbackground=ACCENT2)
            sb.pack()

        # ── Click Options ──────────────────────
        self._section(left, "🖱  Click Options")
        opts_frame = self._panel(left)

        # Mouse button
        btn_col = tk.Frame(opts_frame, bg=BG_PANEL)
        btn_col.pack(side="left", padx=10, pady=6)
        tk.Label(btn_col, text="Mouse Button", font=("Consolas", 8),
                 bg=BG_PANEL, fg=FG_DIM).pack(anchor="w")
        self.mouse_button_var = tk.StringVar(value="Left")
        mb_combo = tk.OptionMenu(btn_col, self.mouse_button_var,
                                 "Left", "Right", "Middle")
        mb_combo.config(font=FONT_MAIN, bg=BG_WIDGET, fg=FG_PRIMARY,
                        activebackground=ACCENT2, activeforeground=FG_PRIMARY,
                        highlightthickness=0, relief="flat", width=7,
                        indicatoron=True, bd=0)
        mb_combo["menu"].config(font=FONT_MAIN, bg=BG_WIDGET, fg=FG_PRIMARY,
                                activebackground=ACCENT2, activeforeground=FG_PRIMARY,
                                bd=0)
        mb_combo.pack()

        # Click type
        type_col = tk.Frame(opts_frame, bg=BG_PANEL)
        type_col.pack(side="left", padx=10, pady=6)
        tk.Label(type_col, text="Click Type", font=("Consolas", 8),
                 bg=BG_PANEL, fg=FG_DIM).pack(anchor="w")
        self.click_type_var = tk.StringVar(value="Single")
        ct_combo = tk.OptionMenu(type_col, self.click_type_var,
                                 "Single", "Double", "Hold",
                                 command=lambda _value: self._toggle_click_type())
        ct_combo.config(font=FONT_MAIN, bg=BG_WIDGET, fg=FG_PRIMARY,
                        activebackground=ACCENT2, activeforeground=FG_PRIMARY,
                        highlightthickness=0, relief="flat", width=7,
                        indicatoron=True, bd=0)
        ct_combo["menu"].config(font=FONT_MAIN, bg=BG_WIDGET, fg=FG_PRIMARY,
                                activebackground=ACCENT2, activeforeground=FG_PRIMARY,
                                bd=0)
        ct_combo.pack()

        # Hold duration
        hold_col = tk.Frame(opts_frame, bg=BG_PANEL)
        hold_col.pack(side="left", padx=10, pady=6)
        tk.Label(hold_col, text="Hold (ms)", font=("Consolas", 8),
                 bg=BG_PANEL, fg=FG_DIM).pack(anchor="w")
        self.hold_ms_var = tk.StringVar(value="500")
        self.hold_spin = tk.Spinbox(
            hold_col, from_=1, to=10_000, width=7,
            textvariable=self.hold_ms_var, font=FONT_BOLD,
            bg=BG_WIDGET, fg=FG_PRIMARY, insertbackground=FG_PRIMARY,
            disabledbackground=BG_WIDGET, disabledforeground=FG_PRIMARY,
            buttonbackground=BG_WIDGET, relief="flat", state="disabled")
        self.hold_spin.pack()

        # ── Click Position ─────────────────────
        self._section(left, "📍  Click Position")
        pos_frame = self._panel(left)

        self.pos_mode_var = tk.StringVar(value="current")
        rb_cur = tk.Radiobutton(pos_frame, text="Current cursor position",
                                variable=self.pos_mode_var, value="current",
                                font=FONT_MAIN, bg=BG_PANEL, fg=FG_PRIMARY,
                                selectcolor=BG_WIDGET, activebackground=BG_PANEL,
                                command=self._toggle_pos_mode)
        rb_cur.pack(anchor="w", padx=8, pady=(6, 2))

        rb_fix = tk.Radiobutton(pos_frame, text="Fixed position (X / Y)",
                                variable=self.pos_mode_var, value="fixed",
                                font=FONT_MAIN, bg=BG_PANEL, fg=FG_PRIMARY,
                                selectcolor=BG_WIDGET, activebackground=BG_PANEL,
                                command=self._toggle_pos_mode)
        rb_fix.pack(anchor="w", padx=8)

        coord_row = tk.Frame(pos_frame, bg=BG_PANEL)
        coord_row.pack(fill="x", padx=8, pady=6)
        tk.Label(coord_row, text="X:", font=FONT_BOLD, bg=BG_PANEL,
                 fg=FG_DIM).pack(side="left")
        self.pos_x_var = tk.StringVar(value="0")
        self.pos_x_entry = tk.Entry(coord_row, textvariable=self.pos_x_var,
                                    width=6, font=FONT_BOLD, bg=BG_WIDGET,
                                    fg=FG_PRIMARY, insertbackground=FG_PRIMARY,
                                    disabledbackground=BG_WIDGET,
                                    disabledforeground=FG_PRIMARY,
                                    relief="flat", state="disabled")
        self.pos_x_entry.pack(side="left", padx=4)
        tk.Label(coord_row, text="Y:", font=FONT_BOLD, bg=BG_PANEL,
                 fg=FG_DIM).pack(side="left")
        self.pos_y_var = tk.StringVar(value="0")
        self.pos_y_entry = tk.Entry(coord_row, textvariable=self.pos_y_var,
                                    width=6, font=FONT_BOLD, bg=BG_WIDGET,
                                    fg=FG_PRIMARY, insertbackground=FG_PRIMARY,
                                    disabledbackground=BG_WIDGET,
                                    disabledforeground=FG_PRIMARY,
                                    relief="flat", state="disabled")
        self.pos_y_entry.pack(side="left", padx=4)
        self.capture_btn = tk.Button(coord_row, text="Capture (3s)",
                                     font=FONT_MAIN, bg=ACCENT2, fg=FG_PRIMARY,
                                     relief="flat", padx=6, cursor="hand2",
                                     state="disabled",
                                     command=self._start_capture)
        self.capture_btn.pack(side="left", padx=6)

        self.capture_label = tk.Label(pos_frame, text="", font=FONT_MONO,
                                      bg=BG_PANEL, fg=FG_GREEN)
        self.capture_label.pack(anchor="w", padx=8)

        # ── Repeat Options (right column) ──────
        self._section(right, "🔁  Repeat")
        rep_frame = self._panel(right)

        self.repeat_var = tk.StringVar(value="infinite")
        modes = [
            ("Run until stopped", "infinite"),
            ("Repeat N times",    "count"),
            ("Run for duration",  "duration"),
        ]
        for text, val in modes:
            rb = tk.Radiobutton(rep_frame, text=text,
                                variable=self.repeat_var, value=val,
                                font=FONT_MAIN, bg=BG_PANEL, fg=FG_PRIMARY,
                                selectcolor=BG_WIDGET, activebackground=BG_PANEL,
                                command=self._toggle_repeat_mode)
            rb.pack(anchor="w", padx=8, pady=2)

        # Count row
        count_row = tk.Frame(rep_frame, bg=BG_PANEL)
        count_row.pack(fill="x", padx=8, pady=4)
        tk.Label(count_row, text="Times:", font=FONT_MAIN, bg=BG_PANEL,
                 fg=FG_DIM).pack(side="left")
        self.repeat_count_var = tk.StringVar(value="10")
        self.count_spin = tk.Spinbox(count_row, from_=1, to=9_999_999,
                                     textvariable=self.repeat_count_var,
                                     width=8, font=FONT_BOLD, bg=BG_WIDGET,
                                     fg=FG_PRIMARY, insertbackground=FG_PRIMARY,
                                     disabledbackground=BG_WIDGET,
                                     disabledforeground=FG_PRIMARY,
                                     buttonbackground=BG_WIDGET,
                                     relief="flat", state="disabled")
        self.count_spin.pack(side="left", padx=6)

        # Duration row
        dur_row = tk.Frame(rep_frame, bg=BG_PANEL)
        dur_row.pack(fill="x", padx=8, pady=4)
        tk.Label(dur_row, text="Duration  H:", font=FONT_MAIN,
                 bg=BG_PANEL, fg=FG_DIM).pack(side="left")
        self.dur_h_var = tk.StringVar(value="0")
        self.dur_h_spin = tk.Spinbox(dur_row, from_=0, to=23,
                                     textvariable=self.dur_h_var,
                                     width=3, font=FONT_BOLD, bg=BG_WIDGET,
                                     fg=FG_PRIMARY, insertbackground=FG_PRIMARY,
                                     disabledbackground=BG_WIDGET,
                                     disabledforeground=FG_PRIMARY,
                                     buttonbackground=BG_WIDGET,
                                     relief="flat", state="disabled")
        self.dur_h_spin.pack(side="left", padx=2)
        tk.Label(dur_row, text="M:", font=FONT_MAIN, bg=BG_PANEL,
                 fg=FG_DIM).pack(side="left")
        self.dur_m_var = tk.StringVar(value="0")
        self.dur_m_spin = tk.Spinbox(dur_row, from_=0, to=59,
                                     textvariable=self.dur_m_var,
                                     width=3, font=FONT_BOLD, bg=BG_WIDGET,
                                     fg=FG_PRIMARY, insertbackground=FG_PRIMARY,
                                     disabledbackground=BG_WIDGET,
                                     disabledforeground=FG_PRIMARY,
                                     buttonbackground=BG_WIDGET,
                                     relief="flat", state="disabled")
        self.dur_m_spin.pack(side="left", padx=2)
        tk.Label(dur_row, text="S:", font=FONT_MAIN, bg=BG_PANEL,
                 fg=FG_DIM).pack(side="left")
        self.dur_s_var = tk.StringVar(value="30")
        self.dur_s_spin = tk.Spinbox(dur_row, from_=0, to=59,
                                     textvariable=self.dur_s_var,
                                     width=3, font=FONT_BOLD, bg=BG_WIDGET,
                                     fg=FG_PRIMARY, insertbackground=FG_PRIMARY,
                                     disabledbackground=BG_WIDGET,
                                     disabledforeground=FG_PRIMARY,
                                     buttonbackground=BG_WIDGET,
                                     relief="flat", state="disabled")
        self.dur_s_spin.pack(side="left", padx=2)

        # ── Start / Stop button ────────────────
        self._section(right, "▶  Control")
        ctrl_panel = self._panel(right)
        self.start_stop_btn = tk.Button(ctrl_panel, text="▶  START",
                                        font=("Consolas", 12, "bold"),
                                        bg=FG_GREEN, fg=BG_DARK,
                                        relief="flat", padx=20, pady=8,
                                        cursor="hand2",
                                        command=self.toggle_clicking)
        self.start_stop_btn.pack(pady=10)

        reset_btn = tk.Button(ctrl_panel, text="Reset Counter",
                              font=FONT_MAIN, bg=BG_WIDGET, fg=FG_DIM,
                              relief="flat", padx=8, pady=4, cursor="hand2",
                              command=self._reset_counter)
        reset_btn.pack(pady=(0, 8))

        # ── Activity Log ───────────────────────
        log_header = tk.Frame(root, bg=BG_DARK)
        log_header.pack(fill="x", padx=16, pady=(4, 0))
        self._section_label(log_header, "📋  Activity Log")
        clear_btn = tk.Button(log_header, text="Clear", font=FONT_MAIN,
                              bg=BG_WIDGET, fg=FG_DIM, relief="flat",
                              padx=6, cursor="hand2",
                              command=self._clear_log)
        clear_btn.pack(side="right")

        log_frame = tk.Frame(root, bg=BG_PANEL)
        log_frame.pack(fill="both", padx=16, pady=(0, 12), ipady=4)

        self.log_text = tk.Text(log_frame, height=7, font=FONT_MONO,
                                bg=BG_PANEL, fg=FG_PRIMARY,
                                insertbackground=FG_PRIMARY,
                                relief="flat", state="disabled",
                                wrap="word")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

    # ── Section label helpers ─────────────────
    def _section(self, parent, text):
        tk.Label(parent, text=text, font=FONT_BOLD, bg=BG_DARK,
                 fg=ACCENT).pack(anchor="w", pady=(10, 2))

    def _section_label(self, parent, text):
        tk.Label(parent, text=text, font=FONT_BOLD, bg=BG_DARK,
                 fg=ACCENT).pack(side="left")

    def _panel(self, parent):
        f = tk.Frame(parent, bg=BG_PANEL, bd=0, relief="flat",
                     highlightthickness=1, highlightcolor=ACCENT2,
                     highlightbackground=BG_PANEL)
        f.pack(fill="x", pady=2)
        return f

    def _apply_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox",
                        fieldbackground=BG_WIDGET,
                        background=BG_WIDGET,
                        foreground=FG_PRIMARY,
                        selectbackground=ACCENT2,
                        selectforeground=FG_PRIMARY,
                        borderwidth=0)
        style.configure("Vertical.TScrollbar",
                        background=BG_PANEL,
                        troughcolor=BG_DARK,
                        arrowcolor=FG_DIM)

    # ══════════════════════════════════════════
    #  UI TOGGLES
    # ══════════════════════════════════════════
    def _toggle_pos_mode(self):
        self._cancel_capture()
        fixed = (self.pos_mode_var.get() == "fixed"
                 and self.run_state == "stopped")
        state = "normal" if fixed else "disabled"
        self.pos_x_entry.config(state=state)
        self.pos_y_entry.config(state=state)
        self.capture_btn.config(state=state)

    def _toggle_click_type(self):
        state = "normal" if self.click_type_var.get() == "Hold" else "disabled"
        self.hold_spin.config(state=state)

    def _toggle_repeat_mode(self):
        mode = self.repeat_var.get()
        self.count_spin.config(state="normal" if mode == "count" else "disabled")
        for w in (self.dur_h_spin, self.dur_m_spin, self.dur_s_spin):
            w.config(state="normal" if mode == "duration" else "disabled")

    # ══════════════════════════════════════════
    #  CAPTURE POSITION
    # ══════════════════════════════════════════
    def _start_capture(self):
        if self.run_state != "stopped" or self.pos_mode_var.get() != "fixed":
            return
        self._cancel_capture()
        self.capture_btn.config(state="disabled")
        self._do_capture_countdown(3)

    def _do_capture_countdown(self, remaining):
        self._capture_after_id = None
        if (self._closing or self.run_state != "stopped"
                or self.pos_mode_var.get() != "fixed"):
            self._cancel_capture()
            return
        if remaining > 0:
            self.capture_label.config(
                text=f"Move cursor to target… {remaining}s", fg=ACCENT)
            self._capture_after_id = self.root.after(
                1000, self._do_capture_countdown, remaining - 1)
        else:
            try:
                controller = MouseController()
                x, y = controller.position
                self.pos_x_var.set(str(int(x)))
                self.pos_y_var.set(str(int(y)))
                self.capture_label.config(
                    text=f"Captured: ({int(x)}, {int(y)})", fg=FG_GREEN)
                self.log(f"Position captured: X={int(x)}, Y={int(y)}")
            except Exception as exc:
                self.capture_label.config(text="Position capture failed", fg=FG_RED)
                self.log(f"⚠ Position capture failed: {exc}")
            self.capture_btn.config(state="normal")

    def _cancel_capture(self):
        if self._capture_after_id is not None:
            try:
                self.root.after_cancel(self._capture_after_id)
            except tk.TclError:
                pass
            self._capture_after_id = None
        if hasattr(self, "capture_label"):
            self.capture_label.config(text="")

    # ══════════════════════════════════════════
    #  START / STOP LOGIC
    # ══════════════════════════════════════════
    def build_config(self):
        """Read and validate a snapshot of the current UI values."""
        values = {
            "int_h":        self.int_vars[0].get(),
            "int_m":        self.int_vars[1].get(),
            "int_s":        self.int_vars[2].get(),
            "int_ms":       self.int_vars[3].get(),
            "button":       self.mouse_button_var.get(),
            "click_type":   self.click_type_var.get(),
            "hold_ms":      self.hold_ms_var.get(),
            "position_mode": self.pos_mode_var.get(),
            "pos_x":        self.pos_x_var.get(),
            "pos_y":        self.pos_y_var.get(),
            "repeat_mode":  self.repeat_var.get(),
            "repeat_count": self.repeat_count_var.get(),
            "dur_h":        self.dur_h_var.get(),
            "dur_m":        self.dur_m_var.get(),
            "dur_s":        self.dur_s_var.get(),
        }
        return ClickConfig.from_values(values)

    def toggle_clicking(self):
        """Toggle between running and stopped states."""
        if self.run_state == "running":
            self._request_stop("User stopped")
        elif self.run_state == "stopped":
            self._start_clicking()

    def _start_clicking(self):
        if self.run_state != "stopped" or self._closing:
            return
        self._cancel_capture()
        try:
            cfg = self.build_config()
        except ConfigError as exc:
            self.log(f"⚠ Cannot start: {exc}")
            return

        self.active_run_id += 1
        run_id = self.active_run_id
        self.total_clicks = 0
        self.click_count_label.config(text="Clicks: 0")
        self.run_state = "running"
        self._toggle_pos_mode()
        self._session_start = time.monotonic()
        self._set_status("running")

        self.worker = ClickWorker(
            config=cfg,
            run_id=run_id,
            on_click_cb=self._on_click,
            on_done_cb=self._on_worker_done,
        )
        try:
            self.worker.start()
        except Exception as exc:
            self.worker = None
            self.run_state = "stopped"
            self._toggle_pos_mode()
            self._set_status("stopped")
            self.log(f"⚠ Could not start click worker: {exc}")
            return

        interval_text = (f"{cfg.interval_seconds * 1000:g}ms"
                         if cfg.interval_seconds < 1
                         else f"{cfg.interval_seconds:g}s")
        hold_text = (f" | hold: {cfg.hold_seconds * 1000:g}ms"
                     if cfg.click_type == "Hold" else "")
        self.log(f"▶ Started | {cfg.click_type} {cfg.button} action | "
                 f"interval: {interval_text}{hold_text}")

    def _request_stop(self, reason="Stopped"):
        if self.run_state != "running":
            return
        self.run_state = "stopping"
        self._set_status("stopping")
        if self.worker:
            self.worker.stop(reason)

    def _finish_run(self, run_id, reason):
        if run_id != self.active_run_id:
            return
        elapsed = time.monotonic() - getattr(self, "_session_start", time.monotonic())
        h, m, s = seconds_to_hms(elapsed)
        self.worker = None
        self.run_state = "stopped"
        self._toggle_pos_mode()
        self._set_status("stopped")
        if reason and not self._closing:
            self.log(f"■ Stopped — {reason} | runtime: {h:02d}:{m:02d}:{s:02d} | clicks: {self.total_clicks:,}")

    def _set_status(self, state):
        """Update UI status indicators (must run on main thread)."""
        if state == "running":
            self.status_dot.config(fg=FG_GREEN)
            self.status_label.config(text=" RUNNING", fg=FG_GREEN)
            self.start_stop_btn.config(
                text="■  STOP", bg=FG_RED, fg=FG_PRIMARY, state="normal")
        elif state == "stopping":
            self.status_dot.config(fg=ACCENT)
            self.status_label.config(text=" STOPPING", fg=ACCENT)
            self.start_stop_btn.config(
                text="STOPPING…", bg=BG_WIDGET, fg=FG_DIM, state="disabled")
        else:
            self.status_dot.config(fg=FG_RED)
            self.status_label.config(text=" STOPPED", fg=FG_RED)
            self.start_stop_btn.config(
                text="▶  START", bg=FG_GREEN, fg=BG_DARK, state="normal")

    # ══════════════════════════════════════════
    #  THREAD-SAFE CALLBACKS (via queue)
    # ══════════════════════════════════════════
    def _on_click(self, run_id, count):
        """Called from worker thread — enqueue UI update."""
        self.ui_queue.put(("click", run_id, count))

    def _on_worker_done(self, run_id, reason):
        """Called from worker thread — enqueue stop update."""
        self.ui_queue.put(("done", run_id, reason))

    def _poll_queue(self):
        """Drain the UI queue on the main thread every 50 ms."""
        try:
            while True:
                msg_type, run_id, payload = self.ui_queue.get_nowait()
                if msg_type == "click":
                    if run_id == self.active_run_id:
                        self.total_clicks = payload
                        self.click_count_label.config(text=f"Clicks: {payload:,}")
                elif msg_type == "done":
                    self._finish_run(run_id, payload)
                elif msg_type == "toggle" and not self._closing:
                    self.toggle_clicking()
                elif msg_type == "log":
                    self._write_log(payload)
        except queue.Empty:
            pass
        if not self._closing:
            self.root.after(50, self._poll_queue)

    def log(self, message: str):
        """Thread-safe logger — can be called from any thread."""
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] {message}"
        if threading.current_thread() is threading.main_thread():
            self._write_log(entry)
        else:
            self.ui_queue.put(("log", None, entry))

    def _write_log(self, entry: str):
        self.log_text.config(state="normal")
        self.log_text.insert("end", entry + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _reset_counter(self):
        if self.run_state != "stopped":
            self.log("⚠ Stop clicking before resetting the counter.")
            return
        self.total_clicks = 0
        self.click_count_label.config(text="Clicks: 0")
        self.log("Counter reset.")

    # ══════════════════════════════════════════
    #  GLOBAL HOTKEY (pynput keyboard listener)
    # ══════════════════════════════════════════
    def _start_hotkey_listener(self):
        def on_press(key):
            if key != DEFAULT_HOTKEY:
                return
            # Key-down debounce: only act once per physical press
            if self._hotkey_pressed:
                return
            self._hotkey_pressed = True
            # Time-based debounce guard
            now = time.monotonic()
            if now - self._last_hotkey_time < HOTKEY_DEBOUNCE_SEC:
                return
            self._last_hotkey_time = now
            self.ui_queue.put(("toggle", None, None))

        def on_release(key):
            if key == DEFAULT_HOTKEY:
                self._hotkey_pressed = False

        try:
            self.hotkey_listener = KeyListener(
                on_press=on_press, on_release=on_release, daemon=True)
            self.hotkey_listener.start()
        except Exception as exc:
            self.hotkey_listener = None
            self.log(f"⚠ Global F6 hotkey unavailable: {exc}")

    # ══════════════════════════════════════════
    #  ORDERLY SHUTDOWN
    # ══════════════════════════════════════════
    def _on_close(self):
        if self._closing:
            return
        self._closing = True
        self._cancel_capture()
        if self.hotkey_listener is not None:
            try:
                self.hotkey_listener.stop()
            except Exception:
                pass
        if self.worker and self.worker.is_alive():
            self.run_state = "stopping"
            self._set_status("stopping")
            self.worker.stop("Application closed")
            self._shutdown_deadline = time.monotonic() + 2.0
            self.root.after(10, self._await_shutdown)
        else:
            self.root.destroy()

    def _await_shutdown(self):
        worker_alive = self.worker is not None and self.worker.is_alive()
        if (worker_alive and self._shutdown_deadline is not None
                and time.monotonic() < self._shutdown_deadline):
            self.root.after(25, self._await_shutdown)
            return
        self.root.destroy()


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    root = tk.Tk()
    app = AutoClickerApp(root)
    root.mainloop()
