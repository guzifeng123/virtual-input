from __future__ import annotations

import ctypes
import difflib
import json
import math
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from keyboard_input import (
    InputProfile,
    count_text_actions,
    foreground_window,
    iter_text_actions,
    send_text,
    top_window,
    user32,
)
from ctypes import wintypes


CALIBRATION_SAMPLE = r'''#include <array>
#include <iostream>
#include <string>

// T01: ordinary lines
int first = 1;
int second = 2;

// T02: brackets on one line
int twice(int x) { return (x + x); }
std::array<int, 3> values{1, 2, 3};

// T03: quotes and escapes
std::string text = "hello";
std::string empty = "";
std::string escaped = "say \\\"hi\\\"";
std::string path = "a\\\\b";
std::string symbols = "{[(<>)]}";
char letter = 'x';
char quote = '\'';

// T04: brackets inside comments: { [ ( < " '
/* T05: block comment
   brackets here: { [ (
*/

// T06: indentation and nested blocks
int main() {
    int total = 0;
    if (values[0] < values[1]) {
        total = twice(values[0]);
        if (total > 0) {
            total++;
        }
        total += 2;
    }

    // T07: line after a blank line
    total += (values[1] + values[2]);
    std::cout << text << " " << total << '\\n';
    return 0;
}
// T08: end of input
'''


def _enable_windows_dpi_awareness() -> None:
    """让 Tk 按显示器真实 DPI 渲染，避免 Windows 位图拉伸造成发虚。"""
    if hasattr(ctypes, "windll"):
        try:
            # Windows 10 1703+：每显示器 V2 感知，支持多屏和运行时缩放。
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            return
        except (AttributeError, OSError):
            pass
        try:
            # 较旧 Windows 的回退方案。
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


class AutoTyperApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("IDE 光标自动输入")
        self.root.geometry("960x640")
        self.root.minsize(860, 580)

        self.path_var = tk.StringVar()
        self.countdown_var = tk.StringVar(value="3")
        self.min_interval_var = tk.StringVar(value="0.03")
        self.max_interval_var = tk.StringVar(value="0.08")
        self.special_delay_var = tk.StringVar(value="0.12")
        self.structure_delay_var = tk.StringVar(value="0.25")
        self.skip_auto_closers_var = tk.StringVar(value="")
        self.auto_closer_user_override = False
        self.ide_mode_var = tk.BooleanVar(value=True)
        self.ide_mode_user_override = False
        self.profile_var = tk.StringVar(value="VS Code")
        self.syntax_aware_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="请选择 TXT 文件")
        self.progress_var = tk.StringVar(value="进度：0 / 0")
        self.timer_var = tk.StringVar(value="")
        self.estimate_var = tk.StringVar(value="预计输入时间：请选择 TXT 文件")
        self.adaptive_var = tk.StringVar(value="平台策略：默认逐字符输入")
        self.adaptive_profile_path = Path(__file__).with_name("平台自适应配置.json")

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
        self.root.configure(bg="#edf3f3")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("App.TFrame", background="#edf3f3")
        style.configure("Glass.TFrame", background="#f8fbfb")
        style.configure("Glass.TLabelframe", background="#f8fbfb", bordercolor="#d7e4e5", relief="flat")
        style.configure("Glass.TLabelframe.Label", background="#f8fbfb", foreground="#5e777b", font=("Segoe UI", 8, "bold"))
        style.configure("Title.TLabel", background="#edf3f3", foreground="#183238", font=("Segoe UI", 19, "bold"))
        style.configure("Meta.TLabel", background="#edf3f3", foreground="#6c8387", font=("Segoe UI", 9))
        style.configure("CardTitle.TLabel", background="#f8fbfb", foreground="#183238", font=("Segoe UI", 14, "bold"))
        style.configure("Muted.TLabel", background="#f8fbfb", foreground="#6b8387", font=("Segoe UI", 9))
        style.configure("Field.TLabel", background="#f8fbfb", foreground="#557075", font=("Segoe UI", 9))
        style.configure("TEntry", fieldbackground="#edf3f3", foreground="#183238", insertcolor="#2f8f83", bordercolor="#d2e0e1", lightcolor="#d2e0e1", darkcolor="#d2e0e1", padding=9)
        style.configure("TCombobox", fieldbackground="#edf3f3", foreground="#183238", selectbackground="#cfe5e1", padding=8)
        style.configure("TCheckbutton", background="#f8fbfb", foreground="#41666b", font=("Segoe UI", 9))
        style.configure("Primary.TButton", background="#2f8f83", foreground="#ffffff", font=("Segoe UI", 10, "bold"), padding=(18, 11), borderwidth=0)
        style.map("Primary.TButton", background=[("active", "#3ca79a"), ("disabled", "#a5bcbc")])
        style.configure("Ghost.TButton", background="#e1ecec", foreground="#365a5f", padding=(14, 10), borderwidth=0)
        style.map("Ghost.TButton", background=[("active", "#d4e5e4")])

        outer = ttk.Frame(self.root, style="App.TFrame", padding=(44, 34, 44, 36))
        outer.pack(fill="both", expand=True)
        top = ttk.Frame(outer, style="App.TFrame")
        top.pack(fill="x", pady=(0, 22))
        ttk.Label(top, text="A  光标自动输入", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="IDE INPUT ORCHESTRATOR", style="Meta.TLabel").pack(side="left", padx=(14, 0), pady=(7, 0))
        ttk.Label(top, text="●  SYSTEM READY", foreground="#2f806f", background="#dceeea", padding=(10, 7), font=("Segoe UI", 9, "bold")).pack(side="right")
        ttk.Label(outer, text="将 TXT 内容逐字符发送到当前光标位置 · 不使用剪贴板", style="Meta.TLabel").pack(anchor="w", pady=(1, 0))

        body = ttk.Frame(outer, style="App.TFrame")
        body.pack(fill="both", expand=True, pady=(28, 0))
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(1, weight=1)

        file_card = ttk.LabelFrame(body, text="  SOURCE FILE  ", style="Glass.TLabelframe", padding=22)
        file_card.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 20))
        file_card.columnconfigure(0, weight=1)
        ttk.Label(file_card, text="选择 TXT 文件", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(file_card, text="支持 UTF-8 BOM / UTF-8 / GBK", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 16))
        ttk.Entry(file_card, textvariable=self.path_var).grid(row=2, column=0, sticky="ew")
        ttk.Button(file_card, text="浏览文件", style="Primary.TButton", command=self._choose_file).grid(row=2, column=1, padx=(10, 0))
        file_actions = ttk.Frame(file_card, style="Glass.TFrame")
        file_actions.grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Button(file_actions, text="生成平台校准样本", style="Ghost.TButton", command=self._create_calibration_sample).pack(side="left")
        ttk.Button(file_actions, text="分析网页回传结果", style="Ghost.TButton", command=self._compare_calibration_result).pack(side="left", padx=(8, 0))
        ttk.Label(file_card, text="先生成样本并开始输入；把网页内容另存为 TXT 后再分析差异", style="Muted.TLabel").grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        settings = ttk.LabelFrame(body, text="  TIMING & BEHAVIOR  ", style="Glass.TLabelframe", padding=22)
        settings.grid(row=1, column=0, sticky="nsew", padx=(0, 20))
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="输入节奏", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(settings, text="速度、停顿和编辑器行为", style="Muted.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 18))
        fields = [
            ("倒计时（秒）", self.countdown_var),
            ("字符最小间隔", self.min_interval_var),
            ("字符最大间隔", self.max_interval_var),
            ("空格/回车延迟", self.special_delay_var),
            ("结构符号延迟", self.structure_delay_var),
        ]
        for row, (label, var) in enumerate(fields, start=2):
            ttk.Label(settings, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(settings, textvariable=var, width=12).grid(row=row, column=1, sticky="e", pady=5)
        ttk.Label(settings, text="编辑器预设", style="Field.TLabel").grid(row=7, column=0, sticky="w", pady=(14, 5))
        ttk.Combobox(settings, textvariable=self.profile_var, values=("VS Code", "通用 IDE", "纯文本"), state="readonly", width=14).grid(row=7, column=1, sticky="e", pady=(14, 5))
        ttk.Checkbutton(settings, text="IDE 代码模式", variable=self.ide_mode_var).grid(row=8, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(settings, text="语法感知（实验）", variable=self.syntax_aware_var).grid(row=9, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(settings, text="自动闭合符（校准会自动填充）", style="Field.TLabel").grid(row=10, column=0, sticky="w", pady=(14, 5))
        ttk.Entry(settings, textvariable=self.skip_auto_closers_var, width=12).grid(row=10, column=1, sticky="e", pady=(14, 5))
        ttk.Label(settings, text="例如 }])\"；只对代码上下文生效", style="Muted.TLabel").grid(row=11, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(settings, text="VS Code 请关闭自动补全/格式化；快捷键：F8 暂停 / 继续 · F9 停止", style="Muted.TLabel").grid(row=12, column=0, columnspan=2, sticky="w", pady=(12, 0))

        status = ttk.LabelFrame(body, text="  LIVE RUN STATUS  ", style="Glass.TLabelframe", padding=22)
        status.grid(row=1, column=1, sticky="nsew")
        ttk.Label(status, text="准备开始输入", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 3))
        ttk.Label(status, textvariable=self.status_var, style="Muted.TLabel", wraplength=260).pack(anchor="w", pady=(7, 18))
        ttk.Label(status, textvariable=self.progress_var, foreground="#183238", background="#f8fbfb", font=("Segoe UI", 22, "bold")).pack(anchor="w")
        ttk.Label(status, textvariable=self.timer_var, foreground="#2f8f83", background="#f8fbfb", font=("Segoe UI", 10)).pack(anchor="w", pady=(5, 0))
        ttk.Label(status, textvariable=self.estimate_var, style="Muted.TLabel", wraplength=260).pack(anchor="w", pady=(14, 0))
        ttk.Label(status, textvariable=self.adaptive_var, style="Muted.TLabel", wraplength=260).pack(anchor="w", pady=(8, 0))
        ttk.Separator(status).pack(fill="x", pady=18)
        buttons = ttk.Frame(status, style="Glass.TFrame")
        buttons.pack(fill="x", side="bottom")
        self.start_button = ttk.Button(buttons, text="开始输入", style="Primary.TButton", command=self._start)
        self.start_button.pack(fill="x")
        self.pause_button = ttk.Button(buttons, text="暂停", style="Ghost.TButton", command=self._toggle_pause, state="disabled")
        self.pause_button.pack(fill="x", pady=(8, 0))
        self.stop_button = ttk.Button(buttons, text="停止", style="Ghost.TButton", command=self._stop, state="disabled")
        self.stop_button.pack(fill="x", pady=(8, 0))

        for widget in (self.countdown_var, self.min_interval_var, self.max_interval_var, self.special_delay_var, self.structure_delay_var, self.skip_auto_closers_var):
            widget.trace_add("write", lambda *_: self._refresh_estimate())
        self.skip_auto_closers_var.trace_add("write", self._on_auto_closers_changed)
        self.ide_mode_var.trace_add("write", self._on_ide_mode_changed)
        self.profile_var.trace_add("write", lambda *_: self._refresh_estimate())
        self.syntax_aware_var.trace_add("write", lambda *_: self._refresh_estimate())

    def _choose_file(self) -> None:
        selected = filedialog.askopenfilename(title="选择 TXT 文件", filetypes=[("TXT 文件", "*.txt"), ("所有文件", "*.*")])
        if selected:
            self.path_var.set(selected)
            self.status_var.set("已选择文件，点击开始后请切换到目标网页")
            self._refresh_estimate()

    def _on_ide_mode_changed(self, *_args: object) -> None:
        # Calibration supplies the initial platform choice.  Once the user
        # touches the checkbox, their choice is authoritative for subsequent
        # runs instead of being silently overwritten by the saved profile.
        self.ide_mode_user_override = True
        self._refresh_estimate()

    def _on_auto_closers_changed(self, *_args: object) -> None:
        # An empty field is a deliberate request to disable the saved
        # calibration, so do not merge it with the profile using ``or``.
        self.auto_closer_user_override = True
        self._refresh_estimate()

    def _effective_ide_mode(self, profile: InputProfile) -> bool:
        if profile.name == "plain":
            return False
        if getattr(self, "ide_mode_user_override", False):
            return bool(self.ide_mode_var.get())
        return bool(profile.skip_indentation)

    def _effective_auto_closers(self, profile: InputProfile) -> str:
        if profile.name == "plain":
            return ""
        if getattr(self, "auto_closer_user_override", False):
            return self.skip_auto_closers_var.get()
        return profile.auto_closers

    def _create_calibration_sample(self) -> None:
        """Create a deterministic source file for measuring a web editor's rewrites."""
        target = Path(__file__).with_name("平台校准样本.txt")
        try:
            target.write_text(CALIBRATION_SAMPLE, encoding="utf-8", newline="\n")
        except OSError as exc:
            messagebox.showerror("生成失败", f"无法写入校准样本：{exc}")
            return
        self.path_var.set(str(target))
        self.status_var.set("已生成校准样本；点击开始输入后切换到网页代码框")
        self.adaptive_var.set("平台策略：等待第一次回传分析")
        self._refresh_estimate()

    def _load_adaptive_profile(self) -> dict[str, object]:
        try:
            value = json.loads(self.adaptive_profile_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, UnicodeDecodeError):
            return {}

    def _apply_adaptive_profile(self, profile: InputProfile) -> InputProfile:
        """Apply only explicit profile settings, never guessed text repairs.

        A calibration export can contain deleted or replaced source characters
        (for example an editor may consume ``>`` or a newline).  Treating
        those differences as evidence for automatic overtyping makes the next
        run lose the same characters again.  The saved file remains useful as
        a diagnostic record, while indentation and auto-closer compensation
        require an explicit checkbox/field choice by the user.
        """
        return profile

    def _infer_adaptive_profile(self, expected: str, actual: str) -> dict[str, object]:
        expected = expected.replace("\r\n", "\n").replace("\r", "\n")
        actual = actual.replace("\r\n", "\n").replace("\r", "\n")
        expected_lines = expected.splitlines()
        actual_lines = actual.splitlines()

        # Align by line content without indentation.  A plain zip() becomes
        # useless as soon as an editor inserts one extra blank line or formats
        # one line, which was the source of false adaptive decisions in the
        # previous implementation.
        expected_content = [line.lstrip(" \t") for line in expected_lines]
        actual_content = [line.lstrip(" \t") for line in actual_lines]
        line_matcher = difflib.SequenceMatcher(
            None, expected_content, actual_content, autojunk=False
        )
        indent_samples: list[tuple[int, int]] = []
        for tag, left_start, left_end, right_start, right_end in line_matcher.get_opcodes():
            if tag not in {"equal", "replace"}:
                continue
            for offset in range(min(left_end - left_start, right_end - right_start)):
                left = expected_lines[left_start + offset]
                right = actual_lines[right_start + offset]
                left_indent = len(left) - len(left.lstrip(" \t"))
                right_indent = len(right) - len(right.lstrip(" \t"))
                if right_indent > left_indent:
                    indent_samples.append((left_indent, right_indent))

        # Do not infer auto-closers from a lossy/rewritten result.  A missing
        # source character and an editor-generated closer are indistinguishable
        # in a plain TXT export; enabling the compensation automatically would
        # make the next run drop valid characters.
        return {
            "version": 3,
            "skip_source_indentation": bool(indent_samples),
            "skip_auto_closers": "",
            "auto_closers_verified": False,
            "indent_observations": len(indent_samples),
            "source_line_count": len(expected_lines),
            "returned_line_count": len(actual_lines),
            "source_preservation_required": True,
        }

    def _compare_calibration_result(self) -> None:
        """Compare the returned web-editor text with the exact source sample."""
        selected = filedialog.askopenfilename(
            title="选择网页复制回来的 TXT",
            filetypes=[("TXT 文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not selected:
            return
        try:
            actual = self._read_text(Path(selected)).replace("\r\n", "\n").replace("\r", "\n")
        except (OSError, UnicodeDecodeError) as exc:
            messagebox.showerror("读取失败", f"无法读取回传结果：{exc}")
            return
        expected = CALIBRATION_SAMPLE.replace("\r\n", "\n")
        if actual == expected:
            profile = {"version": 1, "skip_source_indentation": False, "skip_auto_closers": "", "indent_observations": 0, "source_line_count": len(expected.splitlines()), "returned_line_count": len(actual.splitlines())}
            self.adaptive_profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self.skip_auto_closers_var.set("")
            self.adaptive_var.set("平台策略：已验证原样输入，未启用补偿")
            self.status_var.set("校准成功：网页回传与样本完全一致，策略已保存")
            messagebox.showinfo("校准结果", "网页回传内容与样本完全一致。当前设置可以完整保留字符、括号和缩进。")
            return
        profile = self._infer_adaptive_profile(expected, actual)
        self.adaptive_profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.skip_auto_closers_var.set(str(profile["skip_auto_closers"]))
        self.adaptive_var.set(
            "平台策略：已更新 · "
            f"跳过源缩进={'是' if profile['skip_source_indentation'] else '否'} · "
            f"闭合符补偿={profile['skip_auto_closers'] or '无'}"
        )
        self.status_var.set("校准完成：新策略已保存，下次输入自动采用")
        diff = list(difflib.unified_diff(
            expected.splitlines(), actual.splitlines(),
            fromfile="原始样本", tofile="网页回传", lineterm="",
        ))
        preview = "\n".join(diff[:80])
        if len(diff) > 80:
            preview += "\n…差异过多，仅显示前 80 行。"
        messagebox.showwarning(
            "发现平台改写",
            "网页编辑器修改了输入内容。\n\n"
            f"已自动生成并保存新策略：{self.adaptive_profile_path.name}\n"
            "下次点击开始输入会自动采用该策略。\n"
            "请把下面差异和回传文件发给我，我会继续校正规则：\n\n" + preview,
        )

    @staticmethod
    def _effective_character_counts(
        text: str,
        ide_mode: bool,
        skip_auto_closers: str = "",
        profile: InputProfile | None = None,
    ) -> tuple[int, int, int]:
        """Return counts for the exact action stream used by ``send_text``."""

        count = count_text_actions(
            text,
            ide_mode=ide_mode,
            skip_auto_closers=skip_auto_closers,
            profile=profile,
        )
        special_count = 0
        structure_count = 0
        for action in iter_text_actions(
            text,
            ide_mode=ide_mode,
            skip_auto_closers=skip_auto_closers,
            profile=profile,
        ):
            if action.reason == "autoclose":
                # The sender waits for the editor after crossing a generated
                # closer even though the source character itself is not typed.
                structure_count += 1
            if not action.emit or action.char is None:
                continue
            char = action.char
            if char in (" ", "\n", "\t"):
                special_count += 1
            if char in "{}[]":
                structure_count += 1
        return count, special_count, structure_count

    @staticmethod
    def _profile(name: str, syntax_aware: bool = False) -> InputProfile:
        profile = InputProfile.for_name(name, syntax_aware)
        if syntax_aware:
            try:
                import tree_sitter  # type: ignore  # optional runtime dependency
            except ImportError:
                # Basic character strategy remains fully functional when the
                # optional parser bundle is not installed.
                return InputProfile(profile.name, profile.skip_indentation,
                                    profile.auto_closers, profile.auto_quotes,
                                    False, None)
        return profile

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
            profile = self._profile(self.profile_var.get(), self.syntax_aware_var.get())
            profile = self._apply_adaptive_profile(profile)
            skip_auto_closers = self._effective_auto_closers(profile)
            countdown = int(self.countdown_var.get())
            if (not all(math.isfinite(value) and value >= 0 for value in
                         (interval_min, interval_max, special_delay, structure_delay)) or
                    interval_min > interval_max or not 0 <= countdown <= 10):
                raise ValueError
            text = self._read_text(Path(raw_path))
            ide_mode = self._effective_ide_mode(profile)
            if self.profile_var.get() != "纯文本":
                profile = InputProfile(
                    profile.name,
                    ide_mode,
                    skip_auto_closers,
                    profile.auto_quotes,
                    profile.syntax_aware,
                    profile.language,
                )
            else:
                skip_auto_closers = ""
            count, special_count, structure_count = self._effective_character_counts(
                text, ide_mode, skip_auto_closers, profile
            )
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
        profile = self._profile(self.profile_var.get(), self.syntax_aware_var.get())
        profile = self._apply_adaptive_profile(profile)
        if self.profile_var.get() == "纯文本":
            ide_mode = False
            skip_auto_closers = ""
        else:
            # The checkbox is an explicit per-run override.  This makes it
            # possible to preserve unusual source indentation even when a
            # previous calibration observed editor-owned indentation.
            ide_mode = self._effective_ide_mode(profile)
            skip_auto_closers = self._effective_auto_closers(profile)
            profile = InputProfile(profile.name, ide_mode, skip_auto_closers,
                                    profile.auto_quotes, profile.syntax_aware, profile.language)
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
        self.total, _, _ = self._effective_character_counts(
            text, ide_mode, skip_auto_closers, profile
        )
        self.progress = 0
        self.stop_event.clear()
        self.pause_event.set()
        self.foreground_handle = foreground_window()
        self.target_handle = None
        self.countdown = countdown
        self.needs_countdown = True
        self._set_buttons(running=True)
        self.status_var.set("倒计时中：请切换到 IDE 并点击目标输入位置")
        self.progress_var.set(f"进度：0 / {self.total}")
        self._refresh_estimate()
        self.worker = threading.Thread(target=self._worker, args=(text, countdown, interval_min, interval_max, special_delay, ide_mode, structure_delay, skip_auto_closers, profile), daemon=True)
        self.worker.start()

    def _worker(self, text: str, countdown: int, interval_min: float, interval_max: float | None = None, special_delay: float = 0.0, ide_mode: bool = False, structure_delay: float = 0.0, skip_auto_closers: str = "", profile: InputProfile | None = None) -> None:
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
                             progress, self.stop_event.wait, special_delay, ide_mode, structure_delay, skip_auto_closers, profile)
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
                        self._finish(f"输入发送完成 · 已发送字符（{sent}/{self.total}）")
                    else:
                        self._finish(f"输入计数不一致（{sent}/{self.total}）")
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
    _enable_windows_dpi_awareness()
    root = tk.Tk()
    try:
        root.iconname("网页光标自动输入")
    except tk.TclError:
        pass
    AutoTyperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
