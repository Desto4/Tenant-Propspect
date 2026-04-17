"""Load .env and bootstrap Playwright Chromium on first use."""
import os
import sys
import subprocess
import threading


# ── .env loader ──────────────────────────────────────────────────────────────

def _strip_env_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def load_local_env() -> None:
    """Load .env from the repo root even if python-dotenv is unavailable."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    env_path = os.path.normpath(env_path)
    if not os.path.exists(env_path):
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=True)
        return
    except ImportError:
        pass
    try:
        with open(env_path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key:
                    continue
                os.environ[key] = _strip_env_quotes(value)
    except Exception:
        pass


# ── Playwright bootstrap ──────────────────────────────────────────────────────

_PLAYWRIGHT_INSTALL_LOCK = threading.Lock()
_PLAYWRIGHT_CHROMIUM_READY = False


def _is_missing_browser_error(exc: Exception) -> bool:
    msg = str(exc)
    return "BrowserType.launch" in msg and "Executable doesn't exist" in msg


def ensure_playwright_chromium() -> None:
    global _PLAYWRIGHT_CHROMIUM_READY
    if _PLAYWRIGHT_CHROMIUM_READY:
        return
    with _PLAYWRIGHT_INSTALL_LOCK:
        if _PLAYWRIGHT_CHROMIUM_READY:
            return
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=600,
        )
        _PLAYWRIGHT_CHROMIUM_READY = True


def launch_chromium_resilient(playwright_obj, **kwargs):
    """Launch Chromium; auto-install binary on first missing-executable error."""
    try:
        return playwright_obj.chromium.launch(**kwargs)
    except Exception as exc:
        if not _is_missing_browser_error(exc):
            raise
        ensure_playwright_chromium()
        return playwright_obj.chromium.launch(**kwargs)
