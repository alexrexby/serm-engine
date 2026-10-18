"""Isolate the whole test session from the real installation.

1. data/: DATA_DIR/EXPORTS_DIR/DB_PATH are module constants resolved from SERM_DATA_DIR
   at import time, so it is set before any `config.settings` import.
2. Secrets: tests must never see the real .env (an LLM test once made a paid call with the
   owner's key). The env file path is pointed at a non-existent file and secret-looking
   variables are removed from the process environment.
3. Network: only loopback connections are allowed (aiohttp test servers, local Chromium).
"""
import ipaddress
import os
import socket
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="serm-test-"))
os.environ["SERM_DATA_DIR"] = str(_TMP / "data")
_NO_ENV_FILE = _TMP / "absent.env"
# Read by config.settings at import: every module (Settings, core.admin, cli) gets this path at collection
# time, before any fixture runs. The session fixture below stays as a second guard.
os.environ["SERM_ENV_FILE"] = str(_NO_ENV_FILE)

_SECRET_HINTS = ("TOKEN", "KEY", "PASSWORD", "SECRET")
for _name in list(os.environ):
    if any(h in _name for h in _SECRET_HINTS) or _name.startswith(("POLZA_", "LLM_", "OPP_", "SERM_TG_", "SERM_VK_")):
        os.environ.pop(_name, None)
# core.admin resolves the admin token at import; without one it would generate a token and write the env file
os.environ["ADMIN_TOKEN"] = "test-admin-token-0123456789"


def _is_loopback(address) -> bool:
    if isinstance(address, tuple) and address:
        host = address[0]
    else:  # AF_UNIX path
        return True
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _guarded_connect(self, address):
    if not _is_loopback(address):
        raise RuntimeError(f"tests must not use the network: connect to {address!r}")
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if not _is_loopback(address):
        raise RuntimeError(f"tests must not use the network: connect to {address!r}")
    return _real_connect_ex(self, address)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex


@pytest.fixture(autouse=True, scope="session")
def _no_real_env_file():
    """Point every module-level .env path at a file that does not exist."""
    import config.settings as settings

    settings.ENV_FILE = _NO_ENV_FILE
    settings.Settings.env_path = _NO_ENV_FILE
    for module_name, attr in (("core.admin", "ENV_PATH"), ("cli", "ENV_FILE")):
        try:
            module = __import__(module_name, fromlist=[attr])
        except Exception:
            continue
        if hasattr(module, attr):
            setattr(module, attr, _NO_ENV_FILE)
    yield


@pytest.fixture(scope="session")
def chromium():
    """Skip, not fail, where Playwright's Chromium is not installed (`python -m playwright install chromium`)."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        try:
            pw.chromium.launch(headless=True).close()
        except Exception as e:  # no browser binaries installed
            pytest.skip(f"chromium unavailable, run `python -m playwright install chromium`: {str(e).splitlines()[0]}")
