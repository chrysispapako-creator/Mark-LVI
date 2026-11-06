"""Fancy Dashboard tool: health, one launch, no model-supplied commands.

    python -m unittest plugins._fancy_dashboard_test

The loader skips this file because of the leading underscore. Nothing here
opens a socket or starts PowerShell.
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from core import outcome
from plugins import fancy_dashboard as dash


class _Proc:
    def __init__(self, code=None):
        self.returncode = code

    def poll(self):
        return self.returncode


class DashboardTests(unittest.TestCase):
    def setUp(self):
        dash.reset_inflight()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.launcher = self.root / dash.LAUNCHER_NAME
        self.launcher.write_text("# launcher\n", encoding="utf-8")
        self.log = self.root / "launcher.log"
        self.calls = []

    def tearDown(self):
        dash.reset_inflight()
        self.tmp.cleanup()

    def _popen(self, **kwargs):
        self.calls.append(kwargs)
        self.assertFalse(kwargs.get("shell", False))
        return _Proc(code=0)

    def _run(self, action, *, health, browser=None, background=False, port=8511,
             launcher=None, platform_name="Windows", timeout=2.0):
        return dash.execute(
            {"action": action},
            launcher=str(launcher or self.launcher),
            port=port,
            platform_name=platform_name,
            health=health,
            popen=self._popen,
            open_browser=browser if browser is not None else (lambda url: True),
            background=background,
            log_path=self.log,
            readiness_timeout=timeout,
            sleep=lambda _seconds: None,
        )

    def test_phrases_map_to_three_actions(self):
        self.assertEqual(dash.normalize_action("status"), "status")
        self.assertEqual(dash.normalize_action("is Fancy Dashboard running?"), "status")
        self.assertEqual(dash.normalize_action("start Fancy Dashboard"), "start")
        self.assertEqual(dash.normalize_action("open Fancy Dashboard"), "open")
        self.assertIsNone(dash.normalize_action("powershell -enc blah"))

    def test_status_up_does_not_launch(self):
        result = self._run("is Fancy Dashboard running?", health=lambda origin: (True, "ok"))
        self.assertIn("status=done", result)
        self.assertIn("127.0.0.1:8511", result)
        self.assertEqual(self.calls, [])

    def test_status_down_is_a_failure_and_does_not_launch(self):
        result = self._run("status", health=lambda origin: (False, "URLError"))
        self.assertIn("status=failed", result)
        self.assertIn("not answering", result)
        self.assertEqual(self.calls, [])

    def test_start_when_healthy_does_not_launch_again(self):
        result = self._run("start", health=lambda origin: (True, "ok"))
        self.assertIn("status=done", result)
        self.assertIn("launched=no", result)
        self.assertEqual(self.calls, [])

    def test_open_when_healthy_opens_the_browser_only(self):
        opened = []
        result = self._run(
            "open Fancy Dashboard",
            health=lambda origin: (True, "ok"),
            browser=lambda url: opened.append(url) or True,
        )
        self.assertEqual(opened, ["http://127.0.0.1:8511"])
        self.assertIn("browser=opened", result)
        self.assertEqual(self.calls, [])

    def test_start_when_down_uses_the_fixed_argv_once(self):
        seen = {"n": 0}

        def health(origin):
            self.assertEqual(origin, "http://127.0.0.1:8511")
            seen["n"] += 1
            return (seen["n"] > 1, "ok")

        result = self._run("start", health=health)
        self.assertIn("status=done", result)
        self.assertIn("launched=yes", result)
        self.assertEqual(len(self.calls), 1)
        argv = self.calls[0]["args"]
        self.assertEqual(
            argv[1:],
            ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(self.launcher.resolve())],
        )
        self.assertTrue(str(argv[0]).lower().endswith("powershell.exe"))
        self.assertEqual(self.calls[0]["cwd"], str(self.launcher.resolve().parent))

    def test_model_path_and_args_are_ignored(self):
        def health(origin):
            return (True, "ok")

        result = dash.execute(
            {
                "action": "status",
                "path": r"C:\Windows\System32\cmd.exe",
                "args": "Remove-Item -Recurse C:\\",
                "url": "http://evil.example/steal",
            },
            launcher=str(self.launcher),
            port=8511,
            platform_name="Windows",
            health=health,
            popen=self._popen,
            background=False,
            log_path=self.log,
            sleep=lambda _s: None,
        )
        self.assertIn("status=done", result)
        self.assertEqual(self.calls, [])
        self.assertNotIn("cmd.exe", result)
        self.assertNotIn("evil.example", result)

    def test_second_start_does_not_launch_twice(self):
        state = {"n": 0}

        def health(origin):
            state["n"] += 1
            return (state["n"] > 1, "ok")

        first = self._run("start", health=health)
        self.assertIn("launched=yes", first)
        second = self._run("start Fancy Dashboard", health=lambda origin: (True, "ok"))
        self.assertIn("launched=no", second)
        self.assertEqual(len(self.calls), 1)

    def test_inflight_start_is_not_duplicated(self):
        entered = threading.Event()
        release = threading.Event()

        def health(origin):
            if threading.current_thread().name == "fancy-dashboard-ready":
                entered.set()
                release.wait(timeout=3)
                return True, "ok"
            return False, "down"

        first = self._run("start", health=health, background=True, timeout=5)
        self.assertIn("status=started", first)
        self.assertTrue(entered.wait(timeout=2))
        second = self._run("start", health=health, background=True, timeout=5)
        self.assertIn("already in progress", second)
        self.assertEqual(len(self.calls), 1)
        release.set()

    def test_missing_launcher_fails_without_a_process(self):
        missing = self.root / "missing" / dash.LAUNCHER_NAME
        result = self._run(
            "start",
            health=lambda origin: (False, "down"),
            launcher=missing,
        )
        self.assertIn("status=failed", result)
        self.assertIn("not there", result)
        self.assertEqual(self.calls, [])

    def test_bad_launcher_names_are_refused(self):
        with self.assertRaises(dash.DashboardError):
            dash.resolve_launcher(
                r"C:\Windows\System32\cmd.exe", "Windows",
            )
        with self.assertRaises(dash.DashboardError):
            dash.resolve_launcher(
                r"C:\Users\Chris\Desktop\JarvisProjects\Fancy_Dashboard\Launch-FancyDashboard.ps1 & calc.exe",
                "Windows",
            )
        with self.assertRaises(dash.DashboardError):
            dash.resolve_launcher(
                r"\\server\share\Launch-FancyDashboard.ps1", "Windows",
            )

    def test_default_path_is_the_windows_launcher(self):
        self.assertEqual(
            dash.DEFAULT_LAUNCHER,
            r"C:\Users\Chris\Desktop\JarvisProjects\Fancy_Dashboard\Launch-FancyDashboard.ps1",
        )
        resolved = dash.resolve_launcher("", "Linux")
        self.assertEqual(resolved.name, dash.LAUNCHER_NAME)

    def test_port_and_origin_stay_on_loopback(self):
        self.assertEqual(dash.local_origin(None), "http://127.0.0.1:8511")
        self.assertEqual(dash.local_origin("8511"), "http://127.0.0.1:8511")
        with self.assertRaises(dash.DashboardError):
            dash.local_origin("not-a-port")
        with self.assertRaises(dash.DashboardError):
            dash.check_health("http://example.com")

    def test_health_failure_after_launch_is_not_reported_as_done(self):
        result = self._run(
            "open",
            health=lambda origin: (False, "URLError"),
            timeout=0,
        )
        self.assertIn("status=failed", result)
        self.assertNotIn("status=done", result)
        self.assertEqual(len(self.calls), 1)

    def test_non_windows_can_check_and_will_not_launch(self):
        up = self._run(
            "status",
            health=lambda origin: (True, "ok"),
            platform_name="Linux",
        )
        self.assertIn("status=done", up)
        down = self._run(
            "start",
            health=lambda origin: (False, "down"),
            platform_name="Linux",
        )
        self.assertIn("status=failed", down)
        self.assertIn("Windows launcher", down)
        self.assertEqual(self.calls, [])

    def test_plugin_declares_no_shell_parameters(self):
        names = set(dash.PLUGIN["parameters"]["properties"])
        self.assertEqual(names, {"action"})
        from core.plugin_loader import discover_plugins
        registry = discover_plugins(
            Path(__file__).resolve().parent, set(), logger=lambda _m: None,
        )
        self.assertTrue(registry.has("fancy_dashboard"))

    def test_focused_update_list_leaves_memory_and_custom_files(self):
        from memory.update_apply import is_protected, load_manifest
        manifest = load_manifest(
            Path(__file__).resolve().parent.parent / "scripts" / "fancy_dashboard_update_files.txt"
        )
        self.assertIn("plugins/fancy_dashboard.py", manifest)
        for rel in manifest:
            self.assertFalse(is_protected(rel), rel)
            self.assertNotIn("api_keys", rel)
            self.assertNotIn("voice_bridge", rel)
            self.assertNotIn("grok_voice_bridge", rel)
            self.assertFalse(rel.endswith(".ps1") and "Launch-Fancy" in rel)
            self.assertFalse(rel.startswith("memory/store"))
            self.assertNotEqual(rel, "main.py")
        root = Path(__file__).resolve().parent.parent
        missing = [rel for rel in manifest if not (root / rel).is_file()]
        self.assertEqual(missing, [])


class HealthParserTests(unittest.TestCase):
    def test_200_ok_is_healthy_and_other_bodies_are_not(self):
        class _Resp:
            def __init__(self, status, body):
                self.status = status
                self._body = body

            def read(self, _n):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        def opener(status, body):
            def _open(url, timeout):
                self.assertTrue(url.startswith("http://127.0.0.1:8511/_stcore/health"))
                self.assertLessEqual(timeout, 5)
                return _Resp(status, body)
            return _open

        ok, detail = dash.check_health("http://127.0.0.1:8511", urlopen=opener(200, b"ok"))
        self.assertTrue(ok)
        self.assertEqual(detail, "ok")
        bad, _why = dash.check_health("http://127.0.0.1:8511", urlopen=opener(500, b"ok"))
        self.assertFalse(bad)
        weird, _why = dash.check_health("http://127.0.0.1:8511", urlopen=opener(200, b"<html>"))
        self.assertFalse(weird)


if __name__ == "__main__":
    unittest.main()
