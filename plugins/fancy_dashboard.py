"""Open, start, or check the existing Fancy Dashboard.

The dashboard is a separate Streamlit app. This plugin does not change it
and does not invent a command API for it. The only requests it makes are:

    GET http://127.0.0.1:<port>/_stcore/health
    open that same local origin in the browser
    run the allowlisted launcher, with a fixed argument list, if it is down

The model chooses an action: status, start, or open. It does not choose
the path, the URL, the port, or any shell argument. A configured launcher
path is read from plugin settings (empty means the default Windows path)
and is rejected unless the file name is exactly Launch-FancyDashboard.ps1.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from core import outcome

# The install this was written for. Plugin settings may replace the path;
# the model cannot. The file name is the allowlist.
DEFAULT_LAUNCHER = (
    r"C:\Users\Chris\Desktop\JarvisProjects\Fancy_Dashboard"
    r"\Launch-FancyDashboard.ps1"
)
LAUNCHER_NAME = "Launch-FancyDashboard.ps1"
DEFAULT_PORT = 8511
HEALTH_PATH = "/_stcore/health"

HEALTH_TIMEOUT = 2.0
READINESS_TIMEOUT = 25.0
POLL_SECONDS = 0.4

# Characters that would matter if this string were ever handed to a shell.
# It is not — subprocess is given a list — and they are rejected anyway.
_UNSAFE = set("&|;<>`$\n\r\t\x00")
_WIN_ABS = re.compile(r"^[A-Za-z]:[\\/]")

_state = threading.Lock()
_inflight = False


PLUGIN = {
    "name": "fancy_dashboard",
    "description": (
        "Open, start, or check the user's existing Fancy Dashboard on this "
        "machine. Call this when they say 'open Fancy Dashboard', 'start "
        "Fancy Dashboard', or ask 'is Fancy Dashboard running?'. "
        "action=status only checks whether it is answering. "
        "action=start launches it if it is down and does not start a second "
        "copy. action=open does that and then opens the local page. "
        "Do not use open_app, browser_control, or web_search for this. "
        "You cannot pass a path, a URL, or shell arguments; those are fixed."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "status — is it running? | "
                    "start — launch it if it is down | "
                    "open — launch it if it is down, then open the local page"
                ),
            },
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "fancy_dashboard",
    "title": "Fancy Dashboard",
    "fields": [
        {
            "key": "launcher_path",
            "type": "text",
            "label": "Launcher (blank = default Windows path)",
            "default": "",
            "placeholder": DEFAULT_LAUNCHER,
        },
        {
            "key": "port",
            "type": "text",
            "label": "Local port",
            "default": str(DEFAULT_PORT),
            "placeholder": str(DEFAULT_PORT),
        },
    ],
}


class DashboardError(Exception):
    """A request we will not carry out. Nothing was launched."""


def normalize_action(raw: str) -> str | None:
    """Map what the model sent onto status, start, or open.

    A full sentence is accepted so 'is Fancy Dashboard running?' still
    checks health when the model passes the phrase through instead of the
    single word. Anything else is refused. It is never treated as a command.
    """
    text = " ".join(str(raw or "").lower().split())
    if not text:
        return None
    if text in {"status", "check", "health"} or "running" in text or text.startswith("is "):
        return "status"
    if text == "open" or text.startswith("open "):
        return "open"
    if text in {"start", "launch"} or text.startswith("start ") or text.startswith("launch "):
        return "start"
    return None


def local_origin(port: str | int | None) -> str:
    """http://127.0.0.1:<port> and nothing else.

    localhost is folded to 127.0.0.1 so a hosts-file alias cannot move the
    request. No user, no query, no path, no scheme other than http.
    """
    if port is None or str(port).strip() == "":
        number = DEFAULT_PORT
    else:
        text = str(port).strip()
        if not text.isdigit():
            raise DashboardError(f"Port must be a number, not {text!r}.")
        number = int(text)
    if not 1 <= number <= 65535:
        raise DashboardError(f"Port {number} is outside 1–65535.")
    return f"http://127.0.0.1:{number}"


def resolve_launcher(configured: str | None, platform_name: str) -> Path:
    """The one script this tool is willing to start.

    Empty configuration means the default Windows path. A saved override
    must still be an absolute local path whose file name is
    Launch-FancyDashboard.ps1. The model never reaches this function with
    its own string — callers pass the setting, or nothing.
    """
    raw = (configured or "").strip() or DEFAULT_LAUNCHER
    if any(ch in raw for ch in _UNSAFE):
        raise DashboardError("Launcher path contains characters that are not allowed.")
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise DashboardError("Launcher must be a local file, not a network path.")
    name = Path(raw.replace("\\", "/")).name
    if name != LAUNCHER_NAME:
        raise DashboardError(
            f"Only {LAUNCHER_NAME} can be launched. Refusing {name or raw!r}."
        )
    windows_abs = bool(_WIN_ABS.match(raw))
    path = Path(raw)
    if platform_name == "Windows":
        if not path.is_absolute():
            raise DashboardError("Launcher path must be absolute.")
        resolved = path.resolve()
        if resolved.name != LAUNCHER_NAME:
            raise DashboardError(
                f"Only {LAUNCHER_NAME} can be launched. Refusing {resolved.name}."
            )
        return resolved
    if not windows_abs and not path.is_absolute():
        raise DashboardError("Launcher path must be absolute.")
    # On a non-Windows machine a `C:\...` path has no real location. Keep
    # the file name checkable; this path is not executed here.
    if windows_abs:
        return Path(raw.replace("\\", "/"))
    return path


def launcher_argv(launcher: Path, platform_name: str) -> list[str]:
    """Fixed PowerShell invocation. No shell, no extra arguments.

    The launcher path is the last item and is the resolved allowlisted
    file. Nothing from the model is concatenated into this list.
    """
    if platform_name != "Windows":
        raise DashboardError(
            "Starting Fancy Dashboard uses its Windows launcher "
            f"({LAUNCHER_NAME}). This machine is {platform_name}, so it was "
            "not started. A status check still works if it is already up."
        )
    root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    exe = str(candidate) if candidate.is_file() else "powershell.exe"
    return [
        exe,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-File", str(launcher),
    ]


def _no_redirect_opener():
    """Refuse HTTP redirects so a local answer cannot send us elsewhere."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise DashboardError(f"Refusing redirect from the health check to {newurl}.")

    return urllib.request.build_opener(_NoRedirect)


def check_health(origin: str, timeout: float = HEALTH_TIMEOUT, urlopen=None) -> tuple[bool, str]:
    """GET /_stcore/health. True only for HTTP 200 whose body is empty or 'ok'."""
    if not origin.startswith("http://127.0.0.1:"):
        raise DashboardError("Health checks only go to 127.0.0.1.")
    url = origin.rstrip("/") + HEALTH_PATH
    opener = urlopen or _no_redirect_opener().open
    try:
        with opener(url, timeout=timeout) as response:
            code = getattr(response, "status", None) or response.getcode()
            body = response.read(128).decode("utf-8", "replace").strip().lower()
    except DashboardError:
        raise
    except Exception as exc:
        return False, exc.__class__.__name__
    if code == 200 and (body == "" or body == "ok" or body.startswith("ok")):
        return True, "ok"
    return False, f"HTTP {code}"


def _set_inflight(value: bool) -> bool:
    """True when the flag changed to ``value``. A second launch gets False."""
    global _inflight
    with _state:
        if value:
            if _inflight:
                return False
            _inflight = True
            return True
        _inflight = False
        return True


def reset_inflight() -> None:
    """Tests call this. A crashed waiter must not block the next launch."""
    _set_inflight(False)


def _log(player, message: str) -> None:
    print(f"[FancyDashboard] {message}")
    if player is not None:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass


def _say(player, instruction: str) -> None:
    if player is not None and hasattr(player, "request_say"):
        try:
            player.request_say(instruction)
        except Exception:
            pass


def _default_log_path() -> Path:
    try:
        from memory.memory_manager import get_base_dir
        base = get_base_dir()
    except Exception:
        base = Path(__file__).resolve().parent.parent
    return base / "logs" / "fancy_dashboard.log"


def _spawn(argv: list[str], cwd: str, log_path: Path, popen) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(log_path, "ab", buffering=0)
    try:
        log_handle.write(f"\n--- launch {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
        kwargs = dict(
            args=argv,
            cwd=cwd,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = popen(**kwargs)
        return proc, log_handle
    except Exception:
        log_handle.close()
        raise


def _wait_ready(origin, health, proc, timeout, sleep, poll) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    last = "down"
    while True:
        ok, last = health(origin)
        if ok:
            return True, last
        code = proc.poll()
        if code not in (None, 0):
            return False, f"the launcher exited with code {code}"
        if time.monotonic() >= deadline:
            return False, (
                "the dashboard did not answer /_stcore/health before the timeout"
            )
        sleep(poll)


def _finish(action: str, origin: str, launched: bool, browser: bool, player) -> str:
    if action == "open":
        if browser:
            return outcome.done(
                "fancy_dashboard_open", url=origin,
                launched="yes" if launched else "no", browser="opened",
            )
        return outcome.unverified(
            "fancy_dashboard_open",
            "The dashboard is answering, and the browser did not open.",
            url=origin,
        )
    return outcome.done(
        "fancy_dashboard_start", url=origin,
        launched="yes" if launched else "no",
        detail="already running" if not launched else "health ok",
    )


def execute(
    parameters: dict | None,
    *,
    launcher: str | None = None,
    port: str | int | None = None,
    platform_name: str | None = None,
    player=None,
    health=None,
    popen=None,
    open_browser=None,
    background: bool = True,
    log_path: Path | None = None,
    readiness_timeout: float = READINESS_TIMEOUT,
    sleep=time.sleep,
) -> str:
    """Carry out one status / start / open. Never raises.

    ``health``, ``popen`` and ``open_browser`` exist so tests can watch the
    calls. Production uses the real health check, subprocess, and browser.
    Extra keys on ``parameters`` are ignored — a path or an argument sent
    by the model does not change what runs.
    """
    parameters = parameters or {}
    action = normalize_action(parameters.get("action", ""))
    if action is None:
        return outcome.failed(
            "fancy_dashboard",
            "Say status, start, or open. Nothing was launched.",
        )
    platform_name = platform_name or __import__("platform").system()
    health = health or check_health
    popen = popen or subprocess.Popen
    open_browser = open_browser or webbrowser.open

    try:
        origin = local_origin(port)
    except DashboardError as exc:
        return outcome.failed("fancy_dashboard", str(exc))

    try:
        up, detail = health(origin)
    except DashboardError as exc:
        return outcome.failed("fancy_dashboard", str(exc))

    if action == "status":
        if up:
            return outcome.done("fancy_dashboard_status", url=origin, health="ok")
        if _inflight:
            return outcome.unverified(
                "fancy_dashboard_status",
                "It is starting and is not answering yet.",
                url=origin,
            )
        return outcome.failed(
            "fancy_dashboard_status",
            f"Fancy Dashboard is not answering at {origin} ({detail}).",
            url=origin,
        )

    if up:
        opened = False
        if action == "open":
            try:
                opened = bool(open_browser(origin))
            except Exception as exc:
                return outcome.unverified(
                    "fancy_dashboard_open",
                    f"It is running, and the browser could not be opened ({exc}).",
                    url=origin,
                )
        _log(player, f"Fancy Dashboard is already running at {origin}.")
        return _finish(action, origin, launched=False, browser=opened, player=player)

    if not _set_inflight(True):
        return outcome.unverified(
            "fancy_dashboard_start",
            "A launch is already in progress. It was not started again.",
            url=origin,
        )

    try:
        script = resolve_launcher(launcher, platform_name)
        if platform_name == "Windows" and not script.is_file():
            raise DashboardError(
                f"The launcher is not there: {script}. "
                "Fancy Dashboard was not started."
            )
        argv = launcher_argv(script, platform_name)
    except DashboardError as exc:
        _set_inflight(False)
        return outcome.failed("fancy_dashboard_start", str(exc))

    log_file = Path(log_path) if log_path else _default_log_path()
    try:
        proc, log_handle = _spawn(argv, str(script.parent), log_file, popen)
    except Exception as exc:
        _set_inflight(False)
        return outcome.failed(
            "fancy_dashboard_start",
            f"The launcher could not be started ({exc.__class__.__name__}: {exc}).",
        )

    def _complete() -> str:
        try:
            ready, why = _wait_ready(
                origin, health, proc, readiness_timeout, sleep, POLL_SECONDS,
            )
            if not ready:
                _log(player, f"Fancy Dashboard did not come up: {why}. Log: {log_file}")
                return outcome.failed("fancy_dashboard_start", f"{why}. Log: {log_file}", url=origin)
            opened = False
            if action == "open":
                try:
                    opened = bool(open_browser(origin))
                except Exception as exc:
                    return outcome.unverified(
                        "fancy_dashboard_open",
                        f"Health check succeeded and the browser did not open ({exc}).",
                        url=origin,
                    )
            _log(player, f"Fancy Dashboard is up at {origin}.")
            return _finish(action, origin, launched=True, browser=opened, player=player)
        finally:
            try:
                log_handle.close()
            except Exception:
                pass
            _set_inflight(False)

    if not background:
        return _complete()

    def _worker():
        result = _complete()
        if result.startswith("status=done"):
            _say(player, (
                f"Fancy Dashboard is answering at {origin}. "
                "Tell the user, in one sentence in their language, that it is up."
            ))
        else:
            _say(player, result + " Say that to the user in their language. Do not say it succeeded.")

    threading.Thread(target=_worker, name="fancy-dashboard-ready", daemon=True).start()
    return outcome.started("fancy_dashboard_start", url=origin, launcher=script.name)


def _saved(key: str, default: str = "") -> str:
    try:
        from memory.config_manager import get_plugin_setting
        value = get_plugin_setting("fancy_dashboard", key, default)
    except Exception:
        return default
    return default if value is None else str(value)


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Jarvis entry point. Settings come from the plugin panel, not the model."""
    try:
        port = _saved("port", str(DEFAULT_PORT)).strip() or str(DEFAULT_PORT)
        launcher = _saved("launcher_path", "").strip()
        return execute(
            parameters,
            launcher=launcher or None,
            port=port,
            player=player,
        )
    except Exception as exc:
        return outcome.failed(
            "fancy_dashboard",
            f"Fancy Dashboard tool failed: {exc}",
        )
