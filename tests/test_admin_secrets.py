"""
Admin panel secrets (S1/S2 of the 2026-10-18 security review):
- the secrets form never blanks or replaces a stored token without the explicit UI change flag;
- writes go through core/envfile (unknown keys and comments kept, backup made);
- /api/status returns masks, not secret values;
- a generated ADMIN_TOKEN is not logged.
The real .env is never touched: ENV_PATH points into tmp_path.
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

ADMIN_TOKEN = "test-admin-token-0123456789"
# Must be set before import: core.admin resolves the token at import time and would
# otherwise append a fresh one to the real .env
os.environ["ADMIN_TOKEN"] = ADMIN_TOKEN

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import core.admin as admin  # noqa: E402
from core import envfile  # noqa: E402

BOT_TOKEN = "1234567890:AAH-stored-token-value-xxxxxxxxxxxx"
VK_TOKEN = "vk1.a.stored-vk-token-value"



@pytest.fixture(autouse=True)
def _example_profile(monkeypatch):
    """Brand-agnostic tests: run on the example profile, so a public checkout
    (no own profile in profiles/) behaves the same as the owner's."""
    monkeypatch.setenv("SERM_PROFILE", "example-person")
    import core.profile as profile_module
    if hasattr(profile_module.get_profile, "cache_clear"):
        profile_module.get_profile.cache_clear()

@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / "dotenv"
    path.write_text(
        "# owner comment\n"
        f"OPP_BOT_TOKEN={BOT_TOKEN}\n"
        "OPP_CHANNEL=@old_channel\n"
        f"SERM_VK_TOKEN={VK_TOKEN}\n"
        "POLZA_API_KEY=keep-me\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(admin, "ENV_PATH", path)
    saved = dict(os.environ)  # write_env exports the written keys to os.environ
    yield path
    os.environ.clear()
    os.environ.update(saved)


def _call(method, path, **kwargs):
    async def main():
        async with TestClient(TestServer(admin.create_admin_app(), host="127.0.0.1")) as client:
            headers = {"X-Admin-Token": ADMIN_TOKEN}
            r = await getattr(client, method)(path, headers=headers, **kwargs)
            return r.status, await r.json()
    return asyncio.run(main())


def _parsed(path):
    return envfile.read(path)


def test_empty_secret_from_form_keeps_stored_token(env_file):
    """S1: a form submitted with empty secret fields (status not loaded) must not blank the tokens."""
    status, body = _call("post", "/api/secrets", json={
        "OPP_BOT_TOKEN": "", "OPP_CHANNEL": "@new_channel", "OPP_CONTACT": "@contact",
        "SERM_TG_BOT_TOKEN": "", "SERM_TG_CHANNEL_ID": "", "SERM_VK_TOKEN": "",
    })
    assert status == 200 and body["success"]
    env = _parsed(env_file)
    assert env["OPP_BOT_TOKEN"] == BOT_TOKEN
    assert env["SERM_VK_TOKEN"] == VK_TOKEN
    assert env["OPP_CHANNEL"] == "@new_channel"


def test_secret_without_change_flag_is_ignored(env_file):
    """S1: an autofilled value (e.g. the admin password) without the explicit flag is not written."""
    _call("post", "/api/secrets", json={"OPP_BOT_TOKEN": ADMIN_TOKEN, "OPP_CHANNEL": "@old_channel"})
    assert _parsed(env_file)["OPP_BOT_TOKEN"] == BOT_TOKEN


def test_flagged_secret_is_written_and_rest_preserved(env_file):
    new_token = "9876543210:AAH-new-token-value-yyyyyyyyyyyyyy"
    status, body = _call("post", "/api/secrets", json={
        "OPP_BOT_TOKEN": f"  {new_token} ", "__set__OPP_BOT_TOKEN": "1", "OPP_CHANNEL": "@old_channel",
    })
    assert status == 200 and body["success"]
    text = env_file.read_text(encoding="utf-8")
    env = _parsed(env_file)
    assert env["OPP_BOT_TOKEN"] == new_token
    assert env["POLZA_API_KEY"] == "keep-me"          # unknown key kept (220a0c9)
    assert env["SERM_VK_TOKEN"] == VK_TOKEN
    assert text.startswith("# owner comment\n")       # comment and order kept
    assert "__set__" not in text
    assert list(env_file.parent.glob(f"{env_file.name}.bak-*")), "no backup before write"


def test_flagged_empty_secret_is_ignored(env_file):
    _call("post", "/api/secrets", json={"SERM_VK_TOKEN": "   ", "__set__SERM_VK_TOKEN": "1"})
    assert _parsed(env_file)["SERM_VK_TOKEN"] == VK_TOKEN


def test_line_break_in_value_is_rejected(env_file):
    before = env_file.read_text(encoding="utf-8")
    status, body = _call("post", "/api/secrets", json={"OPP_CHANNEL": "@x\nADMIN_TOKEN=evil"})
    assert status == 400 and not body["success"]
    assert env_file.read_text(encoding="utf-8") == before


def test_status_masks_secrets(env_file):
    status, body = _call("get", "/api/status")
    assert status == 200
    dump = repr(body)
    assert BOT_TOKEN not in dump and VK_TOKEN not in dump
    assert body["has_token"] is True
    assert body["opp_bot_token"].endswith(BOT_TOKEN[-4:]) and body["opp_bot_token"].startswith("••••")
    assert body["serm_tg_token"] == ""
    assert body["opp_channel"] == "@old_channel"


def test_unauthenticated_secrets_rejected(env_file):
    async def main():
        async with TestClient(TestServer(admin.create_admin_app(), host="127.0.0.1")) as client:
            r = await client.post("/api/secrets", json={"OPP_CHANNEL": "@x"})
            return r.status
    assert asyncio.run(main()) == 401
    assert _parsed(env_file)["OPP_CHANNEL"] == "@old_channel"


def test_generated_admin_token_not_logged(tmp_path, monkeypatch, caplog):
    """S2: the generated token goes to .env, never to the log."""
    path = tmp_path / "dotenv"
    path.write_text("OPP_CHANNEL=@c\n", encoding="utf-8")
    monkeypatch.setattr(admin, "ENV_PATH", path)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with caplog.at_level(logging.DEBUG, logger="AdminPanel"):
        token = admin.get_or_create_admin_token()
    assert len(token) == 32
    assert _parsed(path) == {"OPP_CHANNEL": "@c", "ADMIN_TOKEN": token}
    assert token not in caplog.text
    assert "ADMIN_TOKEN" in caplog.text
