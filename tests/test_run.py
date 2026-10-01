"""Launcher."""
import unittest

import run


class LauncherTest(unittest.TestCase):
    def test_pillow_is_detected(self):
        # Used to look for a module named "pillow" and reinstall on every launch
        self.assertEqual(run.check_deps(), [])


if __name__ == "__main__":
    unittest.main()
