"""Dependency checks done at startup (deps.py)."""
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

import deps


class MissingTest(unittest.TestCase):
    def test_installed_packages_are_not_missing(self):
        self.assertEqual(deps.missing({"Pillow": ("PIL", "x")}), [])

    def test_missing_package(self):
        self.assertEqual(deps.missing({"nao-existe": ("pacote_que_nao_existe_123", "x")}),
                         ["nao-existe"])

    def test_does_not_import(self):
        # faster-whisper's import can fail when Windows blocks PyAV: checking
        # must not import it (or the app would offer to reinstall every time)
        with mock.patch("builtins.__import__", side_effect=AssertionError("imported")):
            deps.missing(deps.OPTIONAL)


class PipInstallTest(unittest.TestCase):
    def test_success(self):
        with mock.patch.object(subprocess, "run",
                               return_value=SimpleNamespace(returncode=0, stdout="ok", stderr="")) as run:
            self.assertEqual(deps.pip_install(["x"]), (True, "ok"))
        self.assertEqual(run.call_args.args[0][-1], "x")

    def test_externally_managed_python_retries_with_flag(self):
        results = [SimpleNamespace(returncode=1, stdout="", stderr="externally-managed-environment"),
                   SimpleNamespace(returncode=0, stdout="ok", stderr="")]
        with mock.patch.object(subprocess, "run", side_effect=results) as run:
            ok, _ = deps.pip_install(["x"])
        self.assertTrue(ok)
        self.assertIn("--break-system-packages", run.call_args.args[0])

    def test_failure(self):
        with mock.patch.object(subprocess, "run",
                               return_value=SimpleNamespace(returncode=1, stdout="", stderr="sem rede")):
            self.assertEqual(deps.pip_install(["x"]), (False, "sem rede"))


class FfmpegStatusTest(unittest.TestCase):
    def test_states(self):
        with mock.patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            self.assertEqual(deps.ffmpeg_status(), "ok")
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError()):
            self.assertEqual(deps.ffmpeg_status(), "missing")
        blocked = OSError("bloqueado")
        blocked.winerror = 4551
        with mock.patch.object(subprocess, "run", side_effect=blocked):
            self.assertEqual(deps.ffmpeg_status(), "blocked")
        with mock.patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            self.assertEqual(deps.ffmpeg_status(), "error")

    def test_every_problem_has_help(self):
        for state in ("missing", "blocked", "error"):
            self.assertIn(state, deps.FFMPEG_HELP)


if __name__ == "__main__":
    unittest.main()
