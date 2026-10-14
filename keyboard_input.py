"""Windows SendInput helpers for clipboard-free Unicode keyboard input."""

from __future__ import annotations

import ctypes
import math
import random
import sys
from dataclasses import dataclass
from ctypes import wintypes
from typing import Callable, Iterator, Optional


if sys.platform != "win32":
    raise RuntimeError("keyboard_input.py 仅支持 Windows。")


user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D
VK_TAB = 0x09
VK_DELETE = 0x2E
VK_RIGHT = 0x27
VK_END = 0x23
VK_HOME = 0x24
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_BACKSPACE = 0x08


@dataclass(frozen=True)
class InputProfile:
    name: str = "vscode"
    skip_indentation: bool = False
    # Characters the target inserts automatically and that the planner can
    # safely overtake in code context.
    auto_closers: str = ""
    auto_quotes: bool = False
    syntax_aware: bool = False
    language: str | None = None

    @classmethod
    def for_name(cls, name: str, syntax_aware: bool = False, language: str | None = None) -> "InputProfile":
        key = (name or "vscode").strip().lower()
        if key in {"plain", "纯文本"}:
            return cls("plain", False, "", False, syntax_aware, language)
        if key in {"generic", "通用 ide"}:
            # Keep ``>`` in the compatibility profile, but the lexer only
            # pairs ordinary brackets by default.  In C/C++ a ``<`` is very
            # often a comparison operator or an include/template delimiter;
            # treating every pair as an auto-closer corrupts valid source.
            return cls("generic", True, "}])>", False, syntax_aware, language)
        return cls("vscode", False, "", False, syntax_aware, language)


@dataclass(frozen=True)
class TextAction:
    """One source-character action produced by the input planner.

    ``emit`` is false for source indentation that the editor owns and for a
    source closer already present in the editor's auto-close buffer.  The
    latter still needs a cursor movement, represented by ``reason``.  Cleanup
    actions have ``char`` set to ``None`` and remove an unmatched auto-closer
    left behind at end of input.
    """

    char: str | None
    emit: bool = True
    reason: str = "source"


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetAncestor.restype = wintypes.HWND


def foreground_window() -> int:
    """Return the current foreground window handle for diagnostics."""
    return int(user32.GetForegroundWindow() or 0)


def top_window(handle: int) -> int:
    return int(user32.GetAncestor(handle, 2) or handle)


def _send_key(w_vk: int = 0, w_scan: int = 0, flags: int = 0) -> None:
    item = INPUT(
        type=INPUT_KEYBOARD,
        ki=KEYBDINPUT(w_vk, w_scan, flags, 0, 0),
    )
    sent = user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(INPUT))
    if sent != 1:
        error = ctypes.get_last_error()
        raise OSError(error, "SendInput 发送失败。目标程序权限可能高于本工具。" + ctypes.FormatError(error))


def _send_unicode_unit(unit: int) -> None:
    _send_key(w_scan=unit, flags=KEYEVENTF_UNICODE)
    _send_key(w_scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)


_AUTO_PAIRS = {"{": "}", "[": "]", "(": ")"}
_QUOTES = {"'", '"', "`"}


def _indentation_looks_editor_owned(text: str) -> bool:
    """Return whether source indentation is safe to delegate to an editor.

    A saved calibration may say that a platform auto-indents, but that does
    not mean every input file's leading whitespace is disposable.  In
    particular, the supplied sample intentionally contains a seven-space
    base indent and fifteen-space nested lines.  Only a dominant 2/4/8-space
    pattern (or tabs) is treated as editor-owned; unusual indentation remains
    source data even when IDE mode is enabled.
    """

    runs: list[int] = []
    tab_lines = 0
    for line in text.split("\n")[1:]:
        prefix = line[: len(line) - len(line.lstrip(" \t"))]
        if not prefix:
            continue
        if "\t" in prefix:
            tab_lines += 1
        else:
            runs.append(len(prefix))
    if tab_lines and not runs:
        return True
    if not runs:
        return False
    if tab_lines >= len(runs):
        return True
    threshold = max(1, math.ceil(len(runs) * 0.75))
    return any(sum(run % unit == 0 for run in runs) >= threshold for unit in (2, 4, 8))


def _planned_profile(
    profile: Optional[InputProfile],
    ide_mode: bool,
    skip_auto_closers: str | None,
) -> tuple[bool, set[str], bool]:
    """Return the effective indentation and auto-close settings.

    Calls made against the original public API did not pass a profile.  Keep
    those calls source-preserving even when the legacy ``skip_auto_closers``
    argument is populated; the application passes an explicit profile for
    adaptive behaviour.  This avoids silently changing the behaviour of
    existing integrations while giving the new planner an unambiguous opt-in.
    """

    if profile is None:
        return bool(ide_mode), set(), False
    skip_indentation = bool(
        profile.skip_indentation or (ide_mode and profile.name != "plain")
    )
    configured = str(
        profile.auto_closers if skip_auto_closers is None else skip_auto_closers
    )
    if profile.auto_quotes and (skip_auto_closers is None or configured):
        configured += '"\''
    return skip_indentation, set(configured), bool(configured)


def iter_text_actions(
    text: str,
    *,
    ide_mode: bool = False,
    skip_auto_closers: str | None = None,
    profile: Optional[InputProfile] = None,
    cleanup: bool = False,
) -> Iterator[TextAction]:
    """Plan source input while understanding comments, strings and nesting.

    The target editor may insert a matching closer as soon as an opener is
    typed.  A plain character stack is not sufficient: braces in comments or
    quoted strings must not affect that stack.  This small lexer deliberately
    handles the source constructs relevant to keyboard input (line/block
    comments, escaped single/double quoted strings, and ordinary brackets).
    It is not intended to validate a programming language.

    When a configured auto-closer is encountered in the source, the source
    character is not typed a second time; the caller moves the cursor across
    the editor-generated character.  At EOF, unmatched generated closers are
    deleted so malformed-but-intentional source (for example an unfinished
    string in a test fixture) remains byte-for-byte reproducible.
    """

    skip_indentation, configured, auto_close_enabled = _planned_profile(
        profile, ide_mode, skip_auto_closers
    )
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    skip_indentation = skip_indentation and _indentation_looks_editor_owned(normalized)
    pending: list[str] = []
    state = "normal"
    quote: str | None = None
    escaped = False
    comment_intro: str | None = None
    block_previous = ""
    at_line_start = True
    first_line = True

    for index, char in enumerate(normalized):
        # The first line's indentation is source data.  On later lines the
        # editor has already inserted its own indentation after Enter.
        if skip_indentation and not first_line and at_line_start and char in " \t":
            yield TextAction(char, False, "indentation")
            continue

        emit = True
        reason = "source"
        next_char = normalized[index + 1] if index + 1 < len(normalized) else ""

        if state == "line_comment":
            if char == "\n":
                state = "normal"
        elif state == "block_comment":
            if char == "/" and block_previous == "*":
                state = "normal"
            block_previous = char
        elif state == "quoted":
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                if auto_close_enabled and pending and pending[-1] == char:
                    pending.pop()
                    emit = False
                    reason = "autoclose"
                state = "normal"
                quote = None
        else:
            # The second character of // or /* starts the comment.  We keep
            # both delimiter characters in the output.
            if comment_intro is not None:
                state = "line_comment" if comment_intro == "/" else "block_comment"
                block_previous = char if state == "block_comment" else ""
                comment_intro = None
            elif char == "/" and next_char in {"/", "*"}:
                comment_intro = next_char
            elif char in _QUOTES:
                quote = char
                state = "quoted"
                escaped = False
                if auto_close_enabled and char in configured:
                    pending.append(char)
            elif char in _AUTO_PAIRS:
                closer = _AUTO_PAIRS[char]
                if auto_close_enabled and closer in configured:
                    pending.append(closer)
            elif (
                auto_close_enabled
                and char in configured
                and pending
                and pending[-1] == char
            ):
                # The editor has already inserted this closer.  Do not type
                # it again; the sender will press Right to pass it.
                pending.pop()
                emit = False
                reason = "autoclose"

        yield TextAction(char, emit, reason)
        at_line_start = char == "\n"
        if char == "\n":
            first_line = False
            pass

    if cleanup and auto_close_enabled and pending:
        # Generated closers can be moved to a later line by an editor's
        # newline/indentation rule.  Select the suffix from the current
        # cursor to document end instead of guessing whether Delete reaches
        # a closer or source text.  The selection contains only editor-added
        # content because all source actions have already been processed.
        yield TextAction(None, False, "cleanup_suffix")


def count_text_actions(
    text: str,
    *,
    ide_mode: bool = False,
    skip_auto_closers: str | None = None,
    profile: Optional[InputProfile] = None,
) -> int:
    """Return the number of source characters that will be typed."""

    return sum(
        1
        for action in iter_text_actions(
            text,
            ide_mode=ide_mode,
            skip_auto_closers=skip_auto_closers,
            profile=profile,
        )
        if action.emit
    )


def send_text(
    text: str,
    interval: float | tuple[float, float],
    should_pause: Callable[[], None],
    should_stop: Callable[[], bool],
    on_progress: Optional[Callable[[int], None]] = None,
    wait_interval: Optional[Callable[[float], bool]] = None,
    special_delay: float = 0.0,
    ide_mode: bool = False,
    structure_delay: float = 0.0,
    skip_auto_closers: str | None = None,
    profile: Optional[InputProfile] = None,
) -> int:
    """Send text one logical character at a time without touching clipboard.

    ``should_pause`` blocks while paused and returns when resumed. A character
    outside the BMP is encoded as two UTF-16 units, as required by SendInput.
    Passing an ``InputProfile`` enables the context-aware auto-close planner;
    calls without one retain the legacy source-preserving behaviour.
    """
    sent_chars = 0
    if isinstance(interval, tuple):
        interval_min, interval_max = interval
    else:
        interval_min = interval_max = interval
    interval_min, interval_max = sorted((max(0.0, interval_min), max(0.0, interval_max)))
    special_delay = max(0.0, special_delay)
    structure_delay = max(0.0, structure_delay)
    actions = iter_text_actions(
        text,
        ide_mode=ide_mode,
        skip_auto_closers=skip_auto_closers,
        profile=profile,
        cleanup=True,
    )

    def wait_for(seconds: float) -> None:
        if seconds <= 0 or should_stop():
            return
        if wait_interval is not None:
            wait_interval(seconds)
            return
        import time
        deadline = time.monotonic() + seconds
        while not should_stop() and time.monotonic() < deadline:
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))

    for action in actions:
        if should_stop():
            break
        should_pause()
        if should_stop():
            break

        if action.reason == "indentation":
            # The target already inserted indentation after the preceding
            # Enter.  No keyboard event is needed for this source character.
            continue

        if action.reason == "autoclose":
            # Keep the editor-generated closer and advance over it.  This is
            # reliable even when the target does not implement overtype for a
            # typed closing bracket/quote.
            _send_key(w_vk=VK_RIGHT)
            _send_key(w_vk=VK_RIGHT, flags=KEYEVENTF_KEYUP)
            wait_for(structure_delay)
            continue

        if action.reason == "cleanup_suffix":
            # Ctrl+Shift+End selects any editor-generated indentation and
            # closers after the source cursor, regardless of line layout.
            _send_key(w_vk=VK_CONTROL)
            _send_key(w_vk=VK_SHIFT)
            _send_key(w_vk=VK_END)
            _send_key(w_vk=VK_END, flags=KEYEVENTF_KEYUP)
            _send_key(w_vk=VK_SHIFT, flags=KEYEVENTF_KEYUP)
            _send_key(w_vk=VK_CONTROL, flags=KEYEVENTF_KEYUP)
            _send_key(w_vk=VK_BACKSPACE)
            _send_key(w_vk=VK_BACKSPACE, flags=KEYEVENTF_KEYUP)
            continue

        char = action.char
        if char is None:
            continue
        if char == "\n":
            _send_key(w_vk=VK_RETURN)
            _send_key(w_vk=VK_RETURN, flags=KEYEVENTF_KEYUP)
        elif char == "\t":
            _send_key(w_vk=VK_TAB)
            _send_key(w_vk=VK_TAB, flags=KEYEVENTF_KEYUP)
        else:
            encoded = char.encode("utf-16-le", "surrogatepass")
            for index in range(0, len(encoded), 2):
                _send_unicode_unit(int.from_bytes(encoded[index : index + 2], "little"))

        sent_chars += 1
        if on_progress:
            on_progress(sent_chars)
        if not should_stop():
            delay = random.uniform(interval_min, interval_max)
            if char in (" ", "\n", "\t"):
                delay += special_delay
            if char in "{}[]":
                delay += structure_delay
        else:
            delay = 0.0
        wait_for(delay)
    return sent_chars
