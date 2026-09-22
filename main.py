from __future__ import annotations

import ctypes
import math
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from keyboard_input import foreground_window, send_text, top_window, user32
from ctypes import wintypes


class AutoTyperApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("网页光标自动输入")
        self.root.geometry("700x440")
        self.root.minsize(650, 420)

        self.path_var = tk.StringVar()
        self.countdown_var = tk.StringVar(value="3")
        self.min_interval_var = tk.StringVar(value="0.03")
        self.max_interval_var = tk.StringVar(value="0.08")
        self.special_delay_var = tk.StringVar(value="0.12")
        self.structure_delay_var = tk.StringVar(value="0.25")
        self.skip_auto_closers_var = tk.StringVar(value="")
        self.ide_mode_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="请选择 TXT 文件")
        self.progress_var = tk.StringVar(value="进度：0 / 0")
        self.timer_var = tk.StringVar(value="")
        self.estimate_var = tk.StringVar(value="预计输入时间：请选择 TXT 文件")

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.running = False
        self.paused = False
        self.total = 0
        self.progress = 0
        self.foreground_handle: int | None = None
        self.target_handle: int | None = None
        self.needs_countdown = True
        self.countdown = 3
        self.closing = False
        self.hotkey_stop = threading.Event()

        self._build_ui()
        self.root.update_idletasks()
        self.own_handle = top_window(self.root.winfo_id())
        self._register_hotkeys()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(80, self._poll_events)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="网页光标自动输入", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(outer, text="将 TXT 内容逐字符发送到当前光标位置，不使用剪贴板。", foreground="#555").pack(anchor="w", pady=(3, 16))
        ttk.Label(outer, text="F8 暂停/继续 · F9 停止；继续时重新倒计时。Enter 和 Tab 按网页规则生效。", wraplength=640).pack(anchor="w")

        file_row = ttk.Frame(outer)
        file_row.pack(fill="x", pady=4)
        ttk.Label(file_row, text="TXT 文件", width=12).pack(side="left")
        ttk.Entry(file_row, textvariable=self.path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(file_row, text="浏览…", command=self._choose_file).pack(side="left", padx=(8, 0))

        settings = ttk.LabelFrame(outer, text="输入设置", padding=10)
        settings.pack(fill="x", pady=(14, 10))
        ttk.Label(settings, text="倒计时（秒）").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(settings, from_=0, to=10, increment=1, width=8, textvariable=self.countdown_var).grid(row=0, column=1, padx=(8, 24), sticky="w")
        ttk.Label(settings, text="字符间隔范围（秒）").grid(row=0, column=2, sticky="w")
        ttk.Entry(settings, width=8, textvariable=self.min_interval_var).grid(row=0, column=3, padx=(8, 4), sticky="w")
        ttk.Label(settings, text="至").grid(row=0, column=4, sticky="w")
        ttk.Entry(settings, width=8, textvariable=self.max_interval_var).grid(row=0, column=5, padx=(4, 16), sticky="w")
        ttk.Label(settings, text="空格/回车额外延迟（秒）").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(settings, width=8, textvariable=self.special_delay_var).grid(row=1, column=1, padx=(8, 24), pady=(10, 0), sticky="w")
        ttk.Label(settings, text="编码：自动识别 UTF-8 / BOM / GBK").grid(row=1, column=2, columnspan=4, pady=(10, 0), sticky="w")
        ttk.Checkbutton(settings, text="IDE 代码模式（默认开启：回车后跳过源码行首缩进）", variable=self.ide_mode_var).grid(row=2, column=0, columnspan=6, pady=(10, 0), sticky="w")
        ttk.Label(settings, text="{} [] <> 额外延迟（秒）").grid(row=3, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(settings, width=8, textvariable=self.structure_delay_var).grid(row=3, column=1, padx=(8, 24), pady=(10, 0), sticky="w")
        ttk.Label(settings, text="IDE 自动补全后跳过的闭合符号").grid(row=4, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(settings, width=12, textvariable=self.skip_auto_closers_var).grid(row=4, column=1, padx=(8, 24), pady=(10, 0), sticky="w")
        ttk.Label(settings, text="默认留空；确认 IDE 自动补全后再填写，如 }])>").grid(row=4, column=2, columnspan=4, sticky="w", pady=(10, 0))

        status = ttk.Frame(outer)
        status.pack(fill="x", pady=(2, 10))
        ttk.Label(status, textvariable=self.status_var).pack(anchor="w")
        ttk.Label(status, textvariable=self.timer_var, foreground="#1769aa").pack(anchor="w", pady=(3, 0))
        ttk.Label(status, textvariable=self.progress_var).pack(anchor="w", pady=(3, 0))
        ttk.Label(status, textvariable=self.estimate_var, foreground="#1769aa").pack(anchor="w", pady=(3, 0))

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", side="bottom", pady=(10, 0))
        self.start_button = ttk.Button(buttons, text="开始", command=self._start)
        self.start_button.pack(side="left")
        self.pause_button = ttk.Button(buttons, text="暂停", command=self._toggle_pause, state="disabled")
        self.pause_button.pack(side="left", padx=8)
        self.stop_button = ttk.Button(buttons, text="停止", command=self._stop, state="disabled")
        self.stop_button.pack(side="left")

        for widget in (self.countdown_var, self.min_interval_var, self.max_interval_var, self.special_delay_var, self.structure_delay_var, self.skip_auto_closers_var):
            widget.trace_add("write", lambda *_: self._refresh_estimate())
        self.ide_mode_var.trace_add("write", lambda *_: self._refresh_estimate())

    def _choose_file(self) -> None:
        selected = filedialog.askopenfilename(title="选择 TXT 文件", filetypes=[("TXT 文件", "*.txt"), ("所有文件", "*.*")])
        if selected:
            self.path_var.set(selected)
            self.status_var.set("已选择文件，点击开始后请切换到目标网页")
            self._refresh_estimate()

    @staticmethod
    def _effective_character_counts(text: str, ide_mode: bool, skip_auto_closers: str = "") -> tuple[int, int, int]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        count = 0
        special_count = 0
        structure_count = 0
        configured_closers = set(skip_auto_closers)
        pending_closers: list[str] = []
        at_line_start = True
        first_char = True
        for char in normalized:
            if ide_mode and at_line_start and not first_char and char in (" ", "\t"):
                continue
            if ide_mode and char in configured_closers and pending_closers and pending_closers[-1] == char:
                pending_closers.pop()
                continue
            count += 1
            if char in (" ", "\n", "\t"):
                special_count += 1
            if char in "{}[]<>":
                structure_count += 1
            first_char = False
            at_line_start = char == "\n"
            if ide_mode and char in "{[(<":
                closer = {"{": "}", "[": "]", "(": ")", "<": ">"}[char]
                if closer in configured_closers:
                    pending_closers.append(closer)
        return count, special_count, structure_count

    @staticmethod
    def _format_duration(seconds: float) -> str:
        seconds = max(0, int(round(seconds)))
        minutes, remaining = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours} 小时 {minutes} 分 {remaining} 秒"
        if minutes:
            return f"{minutes} 分 {remaining} 秒"
        return f"{remaining} 秒"

    def _refresh_estimate(self) -> None:
        raw_path = self.path_var.get().strip().strip('"')
        if not raw_path or not Path(raw_path).is_file():
            self.estimate_var.set("预计输入时间：请选择 TXT 文件")
            return
        try:
            interval_min = float(self.min_interval_var.get())
            interval_max = float(self.max_interval_var.get())
            special_delay = float(self.special_delay_var.get())
            structure_delay = float(self.structure_delay_var.get())
            skip_auto_closers = self.skip_auto_closers_var.get()
            countdown = int(self.countdown_var.get())
            if (not all(math.isfinite(value) and value >= 0 for value in
                         (interval_min, interval_max, special_delay, structure_delay)) or
                    interval_min > interval_max or not 0 <= countdown <= 10):
                raise ValueError
            text = self._read_text(Path(raw_path))
            count, special_count, structure_count = self._effective_character_counts(text, self.ide_mode_var.get(), skip_auto_closers)
            average_delay = (interval_min + interval_max) / 2
            input_seconds = count * average_delay + special_count * special_delay + structure_count * structure_delay
            total_seconds = input_seconds + countdown
            self.estimate_var.set(
                f"预计输入时间：{self._format_duration(input_seconds)}（平均值） · "
                f"含倒计时约 {self._format_duration(total_seconds)}"
            )
        except (OSError, UnicodeDecodeError, ValueError):
            self.estimate_var.set("预计输入时间：参数或文件暂不可用")

    @staticmethod
    def _read_text(path: Path) -> str:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            return raw.decode("utf-8-sig")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return raw.decode("gbk")
            except UnicodeDecodeError:
                return raw.decode("mbcs")

    def _parse_settings(self) -> tuple[Path, int, float, float, float, float, bool, str] | None:
        raw_path = self.path_var.get().strip().strip('"')
        if not raw_path:
            messagebox.showerror("无法开始", "请先选择 TXT 文件。")
            return None
        path = Path(raw_path)
        if not path.is_file():
            messagebox.showerror("无法开始", "指定文件不存在或不是文件。")
            return None
        try:
            countdown = int(self.countdown_var.get())
            interval_min = float(self.min_interval_var.get())
            interval_max = float(self.max_interval_var.get())
            special_delay = float(self.special_delay_var.get())
            structure_delay = float(self.structure_delay_var.get())
            skip_auto_closers = self.skip_auto_closers_var.get()
            if (not 0 <= countdown <= 10 or
                    not all(math.isfinite(value) and value >= 0 for value in (interval_min, interval_max, special_delay, structure_delay)) or
                    interval_min > interval_max):
                raise ValueError
        except ValueError:
            messagebox.showerror("无法开始", "倒计时需为 0–10 的整数；间隔和额外延迟需为非负数字，且最小间隔不能大于最大间隔。")
            return None
        return path, countdown, interval_min, interval_max, special_delay, structure_delay, self.ide_mode_var.get(), skip_auto_closers

    def _start(self) -> None:
        if self.running:
            return
        settings = self._parse_settings()
        if settings is None:
            return
        path, countdown, interval_min, interval_max, special_delay, structure_delay, ide_mode, skip_auto_closers = settings
        try:
            text = self._read_text(path)
        except (OSError, UnicodeDecodeError) as exc:
            messagebox.showerror("读取失败", f"无法读取文件：{exc}")
            return
        if not text:
            messagebox.showinfo("空文件", "文件内容为空，没有需要输入的文字。")
            return

        self.running = True
        self.paused = False
        self.total, _, _ = self._effective_character_counts(text, ide_mode, skip_auto_closers)
        self.progress = 0
        self.stop_event.clear()
        self.pause_event.set()
        self.foreground_handle = foreground_window()
        self.target_handle = None
        self.countdown = countdown
        self.needs_countdown = True
        self._set_buttons(running=True)
        self.status_var.set("倒计时中：请切换到网页并点击目标输入位置")
        self.progress_var.set(f"进度：0 / {self.total}")
        self._refresh_estimate()
        self.worker = threading.Thread(target=self._worker, args=(text, countdown, interval_min, interval_max, special_delay, ide_mode, structure_delay, skip_auto_closers), daemon=True)
        self.worker.start()

    def _worker(self, text: str, countdown: int, interval_min: float, interval_max: float | None = None, special_delay: float = 0.0, ide_mode: bool = False, structure_delay: float = 0.0, skip_auto_closers: str = "") -> None:
        if interval_max is None:
            interval_max = interval_min
        try:
            last_update = 0.0
            def progress(n: int) -> None:
                nonlocal last_update
                now = time.monotonic()
                if now - last_update >= 0.05 or n == self.total:
                    self.events.put(("progress", n))
                    last_update = now
            sent = send_text(text, (interval_min, interval_max), self._input_gate, self.stop_event.is_set,
                             progress, self.stop_event.wait, special_delay, ide_mode, structure_delay, skip_auto_closers)
            self.events.put(("progress", sent))
            self.events.put(("stopped" if self.stop_event.is_set() else "finished", sent))
        except Exception as exc:  # report native SendInput failures in UI
            self.events.put(("error", str(exc)))

    def _input_gate(self) -> None:
        while not self.stop_event.is_set():
            if not self.pause_event.wait(0.03):
                continue
            if self.needs_countdown:
                self.needs_countdown = False
                deadline = time.monotonic() + self.countdown
                last_remaining = None
                while self.pause_event.is_set() and not self.stop_event.is_set():
                    remaining = max(0, math.ceil(deadline - time.monotonic()))
                    if remaining != last_remaining:
                        self.events.put(("timer", remaining))
                        last_remaining = remaining
                    if remaining == 0:
                        break
                    self.stop_event.wait(0.02)
                if self.stop_event.is_set():
                    return
                if not self.pause_event.is_set():
                    continue
                candidate = foreground_window()
                if not candidate or candidate == self.own_handle:
                    self.pause_event.clear()
                    self.events.put(("autopause", "已暂停：请定位目标网页，然后按 F8 继续"))
                    continue
                self.target_handle = candidate
                self.events.put(("status", "正在输入…（F8 暂停，F9 停止）"))
            if foreground_window() != self.target_handle:
                self.pause_event.clear()
                self.events.put(("autopause", "已暂停：前台窗口已切换，按 F8 或点击继续"))
                continue
            return

    def _toggle_pause(self) -> None:
        if not self.running:
            return
        if not self.pause_event.is_set():
            self.paused = False
            self.needs_countdown = True
            self.pause_event.set()
            self.pause_button.configure(text="暂停")
            self.status_var.set("重新倒计时：请切换到目标网页")
        else:
            self.paused = True
            self.pause_event.clear()
            self.pause_button.configure(text="继续")
            self.status_var.set("已暂停")

    def _stop(self) -> None:
        if self.running:
            self.stop_event.set()
            self.pause_event.set()
            self.status_var.set("正在停止…")
            self.stop_button.configure(state="disabled")

    def _set_buttons(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.pause_button.configure(state="normal" if running else "disabled", text="暂停")
        self.stop_button.configure(state="normal" if running else "disabled")

    def _finish(self, status: str) -> None:
        self.running = False
        self.paused = False
        self.timer_var.set("")
        self.status_var.set(status)
        self._set_buttons(running=False)

    def _poll_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "timer":
                    self.timer_var.set(f"将在 {value} 秒后开始输入")
                elif kind == "hotkey":
                    if value == 1:
                        self._toggle_pause()
                    else:
                        self._stop()
                elif kind == "hotkey_warning":
                    self.status_var.set(str(value))
                elif kind == "status":
                    self.timer_var.set("")
                    if self.pause_event.is_set():
                        self.status_var.set(str(value))
                elif kind == "autopause":
                    if not self.pause_event.is_set():
                        self.paused = True
                        self.pause_button.configure(text="继续")
                        self.timer_var.set("")
                        self.status_var.set(str(value))
                elif kind == "progress":
                    self.progress = int(value)
                    self.progress_var.set(f"进度：{self.progress} / {self.total}")
                elif kind == "finished":
                    sent = int(value)
                    if sent == self.total:
                        self._finish(f"输入完成 · 键盘事件校验通过（{sent}/{self.total}）")
                    else:
                        self._finish(f"输入完成但校验失败（{sent}/{self.total}）")
                elif kind == "stopped":
                    self._finish("已停止")
                elif kind == "error":
                    self._finish("输入失败")
                    if not self.closing:
                        messagebox.showerror("输入失败", str(value))
        except queue.Empty:
            pass
        if self.closing:
            if (self.worker is None or not self.worker.is_alive()) and not self.hotkey_thread.is_alive():
                self.root.destroy()
                return
        self.root.after(30, self._poll_events)

    def _register_hotkeys(self) -> None:
        user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                       wintypes.UINT, wintypes.UINT, wintypes.UINT)
        user32.PeekMessageW.restype = wintypes.BOOL
        self.hotkey_thread = threading.Thread(target=self._hotkey_loop, daemon=True)
        self.hotkey_thread.start()

    def _hotkey_loop(self) -> None:
        registered = []
        msg = wintypes.MSG()
        try:
            for ident, key in ((1, 0x77), (2, 0x78)):
                if user32.RegisterHotKey(None, ident, 0x4000, key):
                    registered.append(ident)
            if len(registered) != 2:
                self.events.put(("hotkey_warning", "部分快捷键被占用，请使用界面按钮"))
            while not self.hotkey_stop.wait(0.02):
                while user32.PeekMessageW(ctypes.byref(msg), wintypes.HWND(-1), 0x0312, 0x0312, 1):
                    self.events.put(("hotkey", int(msg.wParam)))
        finally:
            for ident in registered:
                user32.UnregisterHotKey(None, ident)

    def _close(self) -> None:
        self.closing = True
        self.stop_event.set()
        self.pause_event.set()
        self.hotkey_stop.set()
        self.root.withdraw()


def main() -> None:
    root = tk.Tk()
    try:
        root.iconname("网页光标自动输入")
    except tk.TclError:
        pass
    AutoTyperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
