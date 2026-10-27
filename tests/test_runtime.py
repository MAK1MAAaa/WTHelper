"""Source and frozen process routing must use separate commands and roots."""

from pathlib import Path
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from scripts import auto_chest, gui, packaged_main, runtime


class RuntimeTests(unittest.TestCase):
    def test_state_root_follows_local_app_data_not_executable_directory(self) -> None:
        with patch.dict(os.environ, {"LOCALAPPDATA": "C:/example/local"}):
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", "D:/other/WTHelper.exe"):
                self.assertEqual(runtime.state_root(), Path("C:/example/local/WTHelper"))
    def test_windowed_build_handles_missing_standard_streams(self) -> None:
        with patch.object(packaged_main, "is_frozen", return_value=True), \
             patch.object(sys, "stdin", None), patch.object(sys, "stdout", None), \
             patch.object(sys, "stderr", None):
            packaged_main.prepare_standard_streams()
            streams = (sys.stdin, sys.stdout, sys.stderr)
            try:
                self.assertEqual(sys.stdin.read(), "")
                self.assertEqual(sys.stdout.write("中文"), 2)
                self.assertEqual(sys.stderr.write("中文"), 2)
            finally:
                for stream in streams:
                    stream.close()

    def test_windowed_build_keeps_redirected_worker_streams(self) -> None:
        streams = [MagicMock(), MagicMock(), MagicMock()]
        with patch.object(packaged_main, "is_frozen", return_value=True), \
             patch.object(sys, "stdin", streams[0]), patch.object(sys, "stdout", streams[1]), \
             patch.object(sys, "stderr", streams[2]):
            packaged_main.prepare_standard_streams()
            self.assertIs(sys.stdin, streams[0])
            self.assertIs(sys.stdout, streams[1])
            self.assertIs(sys.stderr, streams[2])
        for stream in streams:
            stream.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")

    def test_source_command_preserves_python_interpreter_and_script(self) -> None:
        script = Path("test-dir") / "tap_boost.py"
        self.assertEqual(runtime.worker_command(script, ["--dry-run"], unbuffered=True,
                                                executable="python.exe"),
                         ["python.exe", "-u", str(script), "--dry-run"])

    def test_frozen_command_runs_own_executable_with_explicit_worker(self) -> None:
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", "C:/app/WTHelper.exe"):
            self.assertEqual(runtime.worker_command(Path("tap_boost.py"), ["--dry-run"]),
                             ["C:/app/WTHelper.exe", "--worker", "tap", "--dry-run"])
            self.assertEqual(runtime.project_root(), Path("C:/app/WTHelper.exe").resolve().parent)
            with self.assertRaises(ValueError):
                runtime.worker_command(Path("unknown.py"), [])

    def test_frozen_external_launch_restores_bundled_dll_directory_on_error(self) -> None:
        kernel = MagicMock()
        kernel.SetDllDirectoryW.return_value = 1
        with patch.object(runtime, "is_frozen", return_value=True), \
             patch.object(runtime.ctypes, "WinDLL", return_value=kernel), \
             patch.object(sys, "_MEIPASS", "C:/bundle", create=True):
            with self.assertRaises(RuntimeError):
                with runtime.external_dll_search():
                    self.assertEqual(kernel.SetDllDirectoryW.call_args.args, (None,))
                    raise RuntimeError("launch failed")
        self.assertEqual(kernel.SetDllDirectoryW.call_args_list[-1].args, ("C:/bundle",))

    def test_frozen_elevation_resets_extraction_environment_and_restores_parent(self) -> None:
        shell = MagicMock()
        seen: list[str | None] = []
        shell.ShellExecuteW.side_effect = lambda *_args: seen.append(
            gui.os.environ.get("PYINSTALLER_RESET_ENVIRONMENT")) or 50
        with patch.object(gui, "is_frozen", return_value=True), \
             patch.object(gui.ctypes, "WinDLL", return_value=shell), \
             patch.dict(gui.os.environ, {"PYINSTALLER_RESET_ENVIRONMENT": "existing"}):
            self.assertTrue(gui.request_elevation())
            self.assertEqual(gui.os.environ["PYINSTALLER_RESET_ENVIRONMENT"], "existing")
        self.assertEqual(seen, ["1"])
        self.assertIsNone(shell.ShellExecuteW.call_args.args[3])

    def test_frozen_elevation_restores_unset_environment_when_launch_fails(self) -> None:
        shell = MagicMock()
        shell.ShellExecuteW.side_effect = OSError("launch failed")
        with patch.object(gui, "is_frozen", return_value=True), \
             patch.object(gui.ctypes, "WinDLL", return_value=shell), \
             patch.dict(gui.os.environ):
            gui.os.environ.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
            with self.assertRaises(OSError):
                gui.request_elevation()
            self.assertNotIn("PYINSTALLER_RESET_ENVIRONMENT", gui.os.environ)

    def test_external_app_launch_uses_system_dll_search_and_restores_after(self) -> None:
        for fails in (False, True):
            with self.subTest(fails=fails):
                kernel = MagicMock()
                kernel.SetDllDirectoryW.return_value = 1
                def launch(*_args, **_kwargs):
                    self.assertEqual(kernel.SetDllDirectoryW.call_args.args, (None,))
                    if fails:
                        raise OSError("launch failed")
                    return "process"
                with patch.object(runtime, "is_frozen", return_value=True), \
                     patch.object(runtime.ctypes, "WinDLL", return_value=kernel), \
                     patch.object(sys, "_MEIPASS", "C:/bundle", create=True), \
                     patch.object(auto_chest.subprocess, "Popen", side_effect=launch):
                    if fails:
                        with self.assertRaises(auto_chest.AutoChestError):
                            auto_chest.launch_app(Path("WuwaTappo.exe"), 19227)
                    else:
                        self.assertEqual(auto_chest.launch_app(Path("WuwaTappo.exe"), 19227), "process")
                self.assertEqual(kernel.SetDllDirectoryW.call_args_list[-1].args, ("C:/bundle",))
