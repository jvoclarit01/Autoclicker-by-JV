"""
AutoClicker Pro - A full-featured desktop auto clicker
Built with Tkinter (UI) + pynput (mouse/keyboard control)

Threading & Stopping Logic:
- A threading.Event (stop_event) acts as the authoritative stop signal.
- The click worker thread uses time.sleep() sliced into small chunks so it
  can react to stop_event quickly without busy-waiting on the event itself.
- The global hotkey listener runs in its own daemon thread (pynput's default).
- Hotkey debouncing is handled via a timestamp + minimum gap check so holding
  F6 down only fires once per press cycle.
- All UI updates from background threads go through Tkinter's thread-safe
  root.after() / queue mechanism so there are no cross-thread widget writes.
"""

import tkinter as tk
from tkinter import ttk, font as tkfont
import threading
import time
import queue
from pynput import mouse, keyboard
from pynput.mouse import Button, Controller as MouseController
from pynput.keyboard import Key, Listener as KeyListener


# ─────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────
APP_TITLE = "AutoClicker Pro"
VERSION   = "v1.0"
DEFAULT_HOTKEY = Key.f6
HOTKEY_DEBOUNCE_SEC = 0.3   # ignore repeated key events within this window

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


# ─────────────────────────────────────────────
#  Click Worker
# ─────────────────────────────────────────────
class ClickWorker(threading.Thread):
    """
    Background thread that performs the actual mouse clicks.

    Timing approach:
      We do NOT use stop_event.wait(interval) as the primary sleep because
      that wakes up only when the event fires, making it impossible to keep
      accurate intervals. Instead we use time.sleep() in small slices (50 ms)
      and check stop_event between slices so the thread exits promptly.
    """

    SLEEP_SLICE = 0.05   # seconds between stop-event checks

    def __init__(self, config, on_click_cb, on_done_cb):
        """
        config  – dict with all click parameters (see AutoClickerApp.build_config)
        on_click_cb(count) – called (thread-safe) after each click
        on_done_cb(reason) – called when clicking finishes
        """
        super().__init__(daemon=True)
        self.config      = config
        self.on_click_cb = on_click_cb
        self.on_done_cb  = on_done_cb
        self.stop_event  = threading.Event()
        self._click_count = 0

    def stop(self):
        """Signal the worker to stop at the next check point."""
        self.stop_event.set()

    # ── internal helpers ──────────────────────
    def _sleep_interruptible(self, duration):
        """
        Sleep for `duration` seconds but wake early if stop_event is set.
        Returns True if we were interrupted, False if we slept fully.
        """
        end = time.monotonic() + duration
        while True:
            if self.stop_event.is_set():
                return True
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(self.SLEEP_SLICE, remaining))

    def _do_click(self, mc, button, double):
        """Perform a single or double click, catching any exceptions."""
        try:
            if self.config["position_mode"] == "fixed":
                mc.position = (self.config["pos_x"], self.config["pos_y"])
            if double:
                mc.click(button, 2)
            else:
                mc.click(button, 1)
        except Exception as exc:
            # Log but don't crash the thread
            self.on_done_cb(f"Error: {exc}")
            self.stop_event.set()

    # ── main loop ────────────────────────────
    def run(self):
        cfg     = self.config
        mc      = MouseController()
        button  = {"Left": Button.left,
                   "Right": Button.right,
                   "Middle": Button.middle}.get(cfg["button"], Button.left)
        double  = (cfg["click_type"] == "Double")
        interval = hms_to_seconds(cfg["int_h"], cfg["int_m"],
                                  cfg["int_s"], cfg["int_ms"])
        interval = max(interval, 0.01)   # floor at 10 ms

        repeat_mode  = cfg["repeat_mode"]   # "infinite" | "count" | "duration"
        target_count = cfg.get("repeat_count", 0)
        target_dur   = hms_to_seconds(cfg.get("dur_h", 0),
                                      cfg.get("dur_m", 0),
                                      cfg.get("dur_s", 0))
        start_time   = time.monotonic()
        done_reason  = None

        # ── initial delay ─────────────────────
        # Give the user time to move focus away from the app window
        # before the first click fires (especially important for
        # "current cursor" mode where clicking Start would self-click).
        START_DELAY = max(interval, 0.3)
        if self._sleep_interruptible(START_DELAY):
            self.on_done_cb("Stopped")
            return

        while not self.stop_event.is_set():
            # ── termination checks ────────────────
            if repeat_mode == "count" and self._click_count >= target_count:
                done_reason = "Reached click limit"
                break
            if repeat_mode == "duration":
                elapsed = time.monotonic() - start_time
                if elapsed >= target_dur:
                    done_reason = "Duration elapsed"
                    break

            # ── click ─────────────────────────
            self._do_click(mc, button, double)
            self._click_count += 1
            self.on_click_cb(self._click_count)

            # ── wait for next click ───────────
            if self._sleep_interruptible(interval):
                break

        if not self.stop_event.is_set():
            self.stop_event.set()
        self.on_done_cb(done_reason or "Stopped")


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
        self.is_running = False
        self.total_clicks = 0
        self.ui_queue: queue.Queue = queue.Queue()

        # hotkey debounce
        self._last_hotkey_time = 0.0
        self._hotkey_pressed   = False   # track key-down state

        # capture mode
        self._capture_after_id = None

        # ── build UI ───────────────────────────
        self._build_ui()
        self._apply_styles()

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
            var = tk.IntVar(value=default)
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
                                 "Single", "Double")
        ct_combo.config(font=FONT_MAIN, bg=BG_WIDGET, fg=FG_PRIMARY,
                        activebackground=ACCENT2, activeforeground=FG_PRIMARY,
                        highlightthickness=0, relief="flat", width=7,
                        indicatoron=True, bd=0)
        ct_combo["menu"].config(font=FONT_MAIN, bg=BG_WIDGET, fg=FG_PRIMARY,
                                activebackground=ACCENT2, activeforeground=FG_PRIMARY,
                                bd=0)
        ct_combo.pack()

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
        self.pos_x_var = tk.IntVar(value=0)
        self.pos_x_entry = tk.Entry(coord_row, textvariable=self.pos_x_var,
                                    width=6, font=FONT_BOLD, bg=BG_WIDGET,
                                    fg=FG_PRIMARY, insertbackground=FG_PRIMARY,
                                    disabledbackground=BG_WIDGET,
                                    disabledforeground=FG_PRIMARY,
                                    relief="flat", state="disabled")
        self.pos_x_entry.pack(side="left", padx=4)
        tk.Label(coord_row, text="Y:", font=FONT_BOLD, bg=BG_PANEL,
                 fg=FG_DIM).pack(side="left")
        self.pos_y_var = tk.IntVar(value=0)
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
        self.repeat_count_var = tk.IntVar(value=10)
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
        self.dur_h_var = tk.IntVar(value=0)
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
        self.dur_m_var = tk.IntVar(value=0)
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
        self.dur_s_var = tk.IntVar(value=30)
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
        fixed = self.pos_mode_var.get() == "fixed"
        state = "normal" if fixed else "disabled"
        self.pos_x_entry.config(state=state)
        self.pos_y_entry.config(state=state)
        self.capture_btn.config(state=state)

    def _toggle_repeat_mode(self):
        mode = self.repeat_var.get()
        self.count_spin.config(state="normal" if mode == "count" else "disabled")
        for w in (self.dur_h_spin, self.dur_m_spin, self.dur_s_spin):
            w.config(state="normal" if mode == "duration" else "disabled")

    # ══════════════════════════════════════════
    #  CAPTURE POSITION
    # ══════════════════════════════════════════
    def _start_capture(self):
        if self.is_running:
            return
        self.capture_btn.config(state="disabled")
        self._do_capture_countdown(3)

    def _do_capture_countdown(self, remaining):
        if remaining > 0:
            self.capture_label.config(
                text=f"Move cursor to target… {remaining}s", fg=ACCENT)
            self._capture_after_id = self.root.after(
                1000, self._do_capture_countdown, remaining - 1)
        else:
            # Capture current mouse position using pynput
            mc = MouseController()
            x, y = mc.position
            self.pos_x_var.set(int(x))
            self.pos_y_var.set(int(y))
            self.capture_label.config(
                text=f"Captured: ({int(x)}, {int(y)})", fg=FG_GREEN)
            self.capture_btn.config(state="normal")
            self.log(f"Position captured: X={int(x)}, Y={int(y)}")

    # ══════════════════════════════════════════
    #  START / STOP LOGIC
    # ══════════════════════════════════════════
    def build_config(self):
        """Snapshot current UI values into a plain dict for the worker."""
        return {
            "int_h":        self.int_vars[0].get(),
            "int_m":        self.int_vars[1].get(),
            "int_s":        self.int_vars[2].get(),
            "int_ms":       self.int_vars[3].get(),
            "button":       self.mouse_button_var.get(),
            "click_type":   self.click_type_var.get(),
            "position_mode": self.pos_mode_var.get(),
            "pos_x":        self.pos_x_var.get(),
            "pos_y":        self.pos_y_var.get(),
            "repeat_mode":  self.repeat_var.get(),
            "repeat_count": self.repeat_count_var.get(),
            "dur_h":        self.dur_h_var.get(),
            "dur_m":        self.dur_m_var.get(),
            "dur_s":        self.dur_s_var.get(),
        }

    def toggle_clicking(self):
        """Toggle between running and stopped states."""
        if self.is_running:
            self._stop_clicking("User stopped")
        else:
            self._start_clicking()

    def _start_clicking(self):
        cfg = self.build_config()
        interval = hms_to_seconds(cfg["int_h"], cfg["int_m"],
                                  cfg["int_s"], cfg["int_ms"])
        if interval < 0.01:
            self.log("⚠ Interval too small (min 10ms). Set to 10ms.")

        self.is_running = True
        self._session_start = time.monotonic()
        self._set_status(True)

        self.worker = ClickWorker(
            config=cfg,
            on_click_cb=self._on_click,
            on_done_cb=self._on_worker_done,
        )
        self.worker.start()
        self.log(f"▶ Started | {cfg['click_type']} {cfg['button']} click | "
                 f"interval: {int(cfg['int_h'])}h {int(cfg['int_m'])}m "
                 f"{int(cfg['int_s'])}s {int(cfg['int_ms'])}ms")

    def _stop_clicking(self, reason=""):
        if self.worker and self.worker.is_alive():
            self.worker.stop()
        elapsed = time.monotonic() - getattr(self, "_session_start", time.monotonic())
        h, m, s = seconds_to_hms(elapsed)
        self.is_running = False
        self._set_status(False)
        if reason:
            self.log(f"■ Stopped — {reason} | runtime: {h:02d}:{m:02d}:{s:02d} | clicks: {self.total_clicks:,}")

    def _set_status(self, running: bool):
        """Update UI status indicators (must run on main thread)."""
        if running:
            self.status_dot.config(fg=FG_GREEN)
            self.status_label.config(text=" RUNNING", fg=FG_GREEN)
            self.start_stop_btn.config(text="■  STOP", bg=FG_RED, fg=FG_PRIMARY)
        else:
            self.status_dot.config(fg=FG_RED)
            self.status_label.config(text=" STOPPED", fg=FG_RED)
            self.start_stop_btn.config(text="▶  START", bg=FG_GREEN, fg=BG_DARK)

    # ══════════════════════════════════════════
    #  THREAD-SAFE CALLBACKS (via queue)
    # ══════════════════════════════════════════
    def _on_click(self, count):
        """Called from worker thread — enqueue UI update."""
        self.ui_queue.put(("click", count))

    def _on_worker_done(self, reason):
        """Called from worker thread — enqueue stop update."""
        self.ui_queue.put(("done", reason))

    def _poll_queue(self):
        """Drain the UI queue on the main thread every 50 ms."""
        try:
            while True:
                msg_type, payload = self.ui_queue.get_nowait()
                if msg_type == "click":
                    self.total_clicks = payload
                    self.click_count_label.config(text=f"Clicks: {payload:,}")
                elif msg_type == "done":
                    if self.is_running:
                        self._stop_clicking(payload)
                elif msg_type == "log":
                    self._write_log(payload)
        except queue.Empty:
            pass
        self.root.after(50, self._poll_queue)

    def log(self, message: str):
        """Thread-safe logger — can be called from any thread."""
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] {message}"
        if threading.current_thread() is threading.main_thread():
            self._write_log(entry)
        else:
            self.ui_queue.put(("log", entry))

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
            # Schedule toggle on the main thread
            self.root.after(0, self.toggle_clicking)

        def on_release(key):
            if key == DEFAULT_HOTKEY:
                self._hotkey_pressed = False

        listener = KeyListener(on_press=on_press, on_release=on_release,
                               daemon=True)
        listener.start()


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    root = tk.Tk()
    app = AutoClickerApp(root)
    root.mainloop()