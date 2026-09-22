"""Windows SendInput helpers for clipboard-free Unicode keyboard input."""

from __future__ import annotations

import ctypes
import random
import sys
from ctypes import wintypes
from typing import Callable, Optional


if sys.platform != "win32":
    raise RuntimeError("keyboard_input.py 仅支持 Windows。")


user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D
VK_TAB = 0x09


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
    skip_auto_closers: str = "",
) -> int:
    """Send text one logical character at a time without touching clipboard.

    ``should_pause`` blocks while paused and returns when resumed. A character
    outside the BMP is encoded as two UTF-16 units, as required by SendInput.
    """
    sent_chars = 0
    if isinstance(interval, tuple):
        interval_min, interval_max = interval
    else:
        interval_min = interval_max = interval
    interval_min, interval_max = sorted((max(0.0, interval_min), max(0.0, interval_max)))
    special_delay = max(0.0, special_delay)
    structure_delay = max(0.0, structure_delay)
    closer_for = {"}": "{", "]": "[", ")": "(", ">": "<"}
    configured_closers = set(skip_auto_closers)
    pending_closers: list[str] = []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    at_line_start = True
    first_char = True
    for char in normalized:
        if should_stop():
            break
        should_pause()
        if should_stop():
            break

        # Most IDEs insert indentation after Enter. In IDE mode, let the IDE
        # own indentation on subsequent lines instead of sending the source
        # line's leading spaces/tabs a second time.
        if ide_mode and at_line_start and not first_char and char in (" ", "\t"):
            continue
        if ide_mode and char in configured_closers and pending_closers and pending_closers[-1] == char:
            pending_closers.pop()
            # The IDE has already produced this closing character. Still wait
            # before the next source character so completion/formatting can
            # settle before more input arrives.
            if structure_delay > 0 and not should_stop():
                if wait_interval is not None:
                    wait_interval(structure_delay)
                else:
                    import time
                    deadline = time.monotonic() + structure_delay
                    while not should_stop() and time.monotonic() < deadline:
                        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
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

        if ide_mode and char in "{[(<":
            closer = {"{": "}", "[": "]", "(": ")", "<": ">"}[char]
            if closer in configured_closers:
                pending_closers.append(closer)

        sent_chars += 1
        first_char = False
        at_line_start = char == "\n"
        if on_progress:
            on_progress(sent_chars)
        if not should_stop():
            delay = random.uniform(interval_min, interval_max)
            if char in (" ", "\n", "\t"):
                delay += special_delay
            if char in "{}[]<>":
                delay += structure_delay
        else:
            delay = 0.0
        if delay > 0 and not should_stop():
            if wait_interval is not None:
                wait_interval(delay)
            else:
                import time
                deadline = time.monotonic() + delay
                while not should_stop() and time.monotonic() < deadline:
                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    return sent_chars
