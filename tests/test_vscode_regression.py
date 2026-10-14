import unittest
from pathlib import Path
from unittest.mock import patch

import keyboard_input as keyboard
from main import AutoTyperApp


class VSCodeRegressionTests(unittest.TestCase):
    def test_cpp_source_is_sent_without_dropping_characters(self):
        source = (Path(__file__).parent / 'fixtures' / 'maze.txt').read_text(encoding='utf-8')
        received = []
        progress = []

        def key(w_vk=0, w_scan=0, flags=0):
            if flags == keyboard.KEYEVENTF_UNICODE:
                received.append(chr(w_scan))
            elif flags == 0:
                received.append({keyboard.VK_RETURN: '\n', keyboard.VK_TAB: '\t'}[w_vk])

        profile = keyboard.InputProfile.for_name('VS Code')
        with patch.object(keyboard, '_send_key', side_effect=key):
            count = keyboard.send_text(source, 0, lambda: None, lambda: False,
                                       on_progress=progress.append, profile=profile)
        self.assertEqual(''.join(received), source)
        self.assertEqual(count, len(source))
        self.assertEqual(progress[-1], len(source))
        self.assertEqual(AutoTyperApp._effective_character_counts(
            source, profile.skip_indentation, profile.auto_closers)[0], count)


if __name__ == '__main__':
    unittest.main()
