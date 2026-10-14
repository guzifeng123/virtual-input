import ctypes
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import keyboard_input as keyboard
from main import AutoTyperApp


class InputTests(unittest.TestCase):
    def test_windows_input_layout(self):
        self.assertEqual(ctypes.sizeof(keyboard.INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)

    def test_encodings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.txt"
            for encoding in ("utf-8", "utf-8-sig", "gbk"):
                with self.subTest(encoding=encoding):
                    path.write_bytes("中文测试\r\nABC".encode(encoding))
                    self.assertEqual(AutoTyperApp._read_text(path), "中文测试\r\nABC")
            path.write_bytes(b"")
            self.assertEqual(AutoTyperApp._read_text(path), "")
            with self.assertRaises(FileNotFoundError):
                AutoTyperApp._read_text(Path(directory) / "absent.txt")

    def test_unicode_and_control_events(self):
        with patch.object(keyboard, "_send_key") as send:
            count = keyboard.send_text("A中😀\r\n\t", 0, lambda: None, lambda: False)
        self.assertEqual(count, 5)
        calls = send.call_args_list
        self.assertEqual(len(calls), 12)
        self.assertEqual([calls[i].kwargs["w_scan"] for i in (0, 2, 4, 6)],
                         [65, 0x4E2D, 0xD83D, 0xDE00])
        self.assertEqual(calls[8].kwargs, {"w_vk": keyboard.VK_RETURN})
        self.assertEqual(calls[10].kwargs, {"w_vk": keyboard.VK_TAB})
        for i in (1, 3, 5, 7):
            self.assertEqual(calls[i].kwargs["flags"], keyboard.KEYEVENTF_UNICODE | keyboard.KEYEVENTF_KEYUP)

    def test_stop_interrupts_long_interval(self):
        stop = threading.Event()
        first = threading.Event()
        result = []
        with patch.object(keyboard, "_send_key") as send:
            worker = threading.Thread(target=lambda: result.append(keyboard.send_text(
                "abc", 60, lambda: None, stop.is_set, lambda n: first.set(), stop.wait)))
            worker.start()
            self.assertTrue(first.wait(1))
            stop.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [1])
        self.assertEqual(send.call_count, 2)

    def test_pause_retains_next_character(self):
        resume = threading.Event()
        waiting = threading.Event()
        result = []
        def gate():
            waiting.set()
            resume.wait(1)
        with patch.object(keyboard, "_send_key") as send:
            worker = threading.Thread(target=lambda: result.append(
                keyboard.send_text("中", 0, gate, lambda: False)))
            worker.start()
            self.assertTrue(waiting.wait(1))
            self.assertEqual(send.call_count, 0)
            resume.set()
            worker.join(1)
        self.assertEqual(result, [1])

    def make_app_state(self):
        app = AutoTyperApp.__new__(AutoTyperApp)
        app.stop_event = threading.Event()
        app.pause_event = threading.Event()
        app.pause_event.set()
        app.events = queue.Queue()
        app.needs_countdown = True
        app.countdown = 0
        app.own_handle = 10
        app.target_handle = None
        app.total = 1
        return app

    def test_own_window_never_receives_input(self):
        app = self.make_app_state()
        with patch("main.foreground_window", return_value=10), patch.object(keyboard, "_send_key") as send:
            worker = threading.Thread(target=app._worker, args=("X", 0, 0))
            worker.start()
            deadline = time.monotonic() + 1
            while app.pause_event.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(app.pause_event.is_set())
            app.stop_event.set()
            app.pause_event.set()
            worker.join(1)
            self.assertEqual(send.call_count, 0)
        events = list(app.events.queue)
        self.assertIn(("stopped", 0), events)

    def test_focus_change_pauses(self):
        app = self.make_app_state()
        app.needs_countdown = False
        app.target_handle = 20
        with patch("main.foreground_window", return_value=30):
            worker = threading.Thread(target=app._input_gate)
            worker.start()
            kind, message = app.events.get(timeout=1)
            self.assertEqual(kind, "autopause")
            self.assertFalse(app.pause_event.is_set())
            app.stop_event.set()
            app.pause_event.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_countdown_can_stop(self):
        app = self.make_app_state()
        app.countdown = 10
        with patch.object(keyboard, "_send_key") as send:
            worker = threading.Thread(target=app._worker, args=("X", 10, 0))
            worker.start()
            self.assertEqual(app.events.get(timeout=1), ("timer", 10))
            app.stop_event.set()
            worker.join(1)
            self.assertEqual(send.call_count, 0)
        self.assertIn(("stopped", 0), list(app.events.queue))

    def test_native_failure_surfaces(self):
        with patch.object(keyboard.user32, "SendInput", return_value=0):
            with self.assertRaises(OSError):
                keyboard._send_key(w_vk=keyboard.VK_RETURN)

    def test_ide_mode_uses_editor_indentation(self):
        sent = []
        def fake_key(w_vk=0, w_scan=0, flags=0):
            if flags == 0 and w_vk == keyboard.VK_RETURN:
                sent.append("ENTER")
            elif flags == keyboard.KEYEVENTF_UNICODE and w_scan:
                sent.append(chr(w_scan))
        with patch.object(keyboard, "_send_key", side_effect=fake_key):
            keyboard.send_text("if (x) {\n    value();\n}", 0,
                               lambda: None, lambda: False, ide_mode=True)
        self.assertEqual("".join(sent), "if (x) {ENTERvalue();ENTER}")

    def test_plain_mode_preserves_indentation(self):
        sent = []
        def fake_key(w_vk=0, w_scan=0, flags=0):
            if flags == 0 and w_vk == keyboard.VK_RETURN:
                sent.append("ENTER")
            elif flags == keyboard.KEYEVENTF_UNICODE and w_scan:
                sent.append(chr(w_scan))
        with patch.object(keyboard, "_send_key", side_effect=fake_key):
            keyboard.send_text("a\n  b", 0, lambda: None, lambda: False, ide_mode=False)
        self.assertEqual("".join(sent), "aENTER  b")

    def test_configurable_auto_closers(self):
        sent = []
        delays = []
        def fake_key(w_vk=0, w_scan=0, flags=0):
            if flags == keyboard.KEYEVENTF_UNICODE and w_scan:
                sent.append(chr(w_scan))
        with patch.object(keyboard, "_send_key", side_effect=fake_key):
            keyboard.send_text("{value}", 0, lambda: None, lambda: False,
                               wait_interval=lambda seconds: delays.append(seconds),
                               ide_mode=True, skip_auto_closers="}", structure_delay=0.25)
        self.assertEqual("".join(sent), "{value}")

        self.assertEqual(delays, [0.25, 0.25])
        sent.clear()
        delays.clear()
        with patch.object(keyboard, "_send_key", side_effect=fake_key):
            keyboard.send_text("{value}", 0, lambda: None, lambda: False, ide_mode=True, skip_auto_closers="")
        self.assertEqual("".join(sent), "{value}")

    def test_context_aware_planner_ignores_comments_and_strings(self):
        profile = keyboard.InputProfile("vscode", False, "})", False)
        source = '// { [ ( "\nstd::string s = "{[(]";\nif (x) {\n  y();\n}'
        actions = list(keyboard.iter_text_actions(source, profile=profile))
        emitted = "".join(a.char for a in actions if a.char is not None and a.emit)
        self.assertEqual(emitted, '// { [ ( "\nstd::string s = "{[(]";\nif (x {\n  y(;\n')
        self.assertEqual([a.char for a in actions if a.reason == "autoclose"], [")", ")", "}"])

    def test_cleanup_selects_generated_suffix(self):
        calls = []
        profile = keyboard.InputProfile("vscode", False, "}", False)
        with patch.object(keyboard, "_send_key", side_effect=lambda **kwargs: calls.append(kwargs)):
            count = keyboard.send_text("if (x) {\n", 0, lambda: None, lambda: False,
                                       profile=profile)
        self.assertEqual(count, len("if (x) {\n"))
        cleanup_calls = [call for call in calls if call.get("w_vk") in {
            keyboard.VK_CONTROL, keyboard.VK_SHIFT, keyboard.VK_END, keyboard.VK_BACKSPACE
        }]
        self.assertEqual(cleanup_calls, [
            {"w_vk": keyboard.VK_CONTROL},
            {"w_vk": keyboard.VK_SHIFT},
            {"w_vk": keyboard.VK_END},
            {"w_vk": keyboard.VK_END, "flags": keyboard.KEYEVENTF_KEYUP},
            {"w_vk": keyboard.VK_SHIFT, "flags": keyboard.KEYEVENTF_KEYUP},
            {"w_vk": keyboard.VK_CONTROL, "flags": keyboard.KEYEVENTF_KEYUP},
            {"w_vk": keyboard.VK_BACKSPACE},
            {"w_vk": keyboard.VK_BACKSPACE, "flags": keyboard.KEYEVENTF_KEYUP},
        ])

    def test_estimate_counts_follow_ide_mode(self):
        text = "if {\n    value();\n}"
        self.assertEqual(AutoTyperApp._effective_character_counts(text, True), (15, 3, 2))
        self.assertEqual(AutoTyperApp._effective_character_counts("{x}", True, "}"), (3, 0, 2))
        self.assertEqual(AutoTyperApp._effective_character_counts(text, False), (19, 7, 2))
        self.assertEqual(AutoTyperApp._format_duration(0), "0 秒")
        self.assertEqual(AutoTyperApp._format_duration(65), "1 分 5 秒")

    def test_random_range_and_special_delay(self):
        delays = []
        with patch.object(keyboard, "_send_key"), patch.object(keyboard.random, "uniform", side_effect=[0.1, 0.2, 0.3, 0.4]):
            keyboard.send_text("a b\n", (0.01, 0.05), lambda: None, lambda: False,
                               wait_interval=lambda seconds: delays.append(seconds), special_delay=0.4)
        self.assertEqual(len(delays), 4)
        for actual, expected in zip(delays, [0.1, 0.6, 0.3, 0.8]):
            self.assertAlmostEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
