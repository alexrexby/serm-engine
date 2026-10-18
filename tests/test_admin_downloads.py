"""
Admin downloads: /download/lead-magnet serves only lead magnet PDFs of the active campaign
(no absolute paths, no `..`), /download/extension requires authentication and builds the zip
in memory on every request; a zip cached by an older version in data/exports is removed at start.
"""
import asyncio
import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

ADMIN_TOKEN = "test-admin-token-0123456789"
os.environ.setdefault("ADMIN_TOKEN", ADMIN_TOKEN)

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import core.admin as admin  # noqa: E402
from config.settings import EXPORTS_DIR  # noqa: E402
from core.profile import get_profile  # noqa: E402

TOKEN = os.environ["ADMIN_TOKEN"]
HEADERS = {"X-Admin-Token": TOKEN}
CACHED_ZIP = EXPORTS_DIR / "serm_cookie_sync_extension.zip"


@pytest.fixture
def example_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("SERM_PROFILE", str(PROJECT_ROOT / "profiles" / "example-person.yaml"))
    monkeypatch.delenv("SERM_ACTIVE_CAMPAIGN", raising=False)
    monkeypatch.setattr(admin, "ENV_PATH", tmp_path / "dotenv")
    get_profile.cache_clear()
    yield get_profile()
    get_profile.cache_clear()


@pytest.fixture
def camp_dir(example_profile):
    path = EXPORTS_DIR / f"campaign_{example_profile.campaign_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run(coro_fn):
    async def main():
        async with TestClient(TestServer(admin.create_admin_app(), host="127.0.0.1")) as client:
            return await coro_fn(client)
    return asyncio.run(main())


def test_lead_magnet_download_stays_in_campaign_dir(camp_dir, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("SECRET-OUTSIDE-CAMPAIGN", encoding="utf-8")
    (camp_dir / "lead_magnet_audit.pdf").write_bytes(b"%PDF-audit")
    (camp_dir / "notes.txt").write_text("not a lead magnet", encoding="utf-8")
    relative = os.path.relpath(secret, camp_dir)
    assert relative.startswith("..")

    async def fetch(client):
        bodies = {}
        for name, query in {
            "absolute": {"file": str(secret)},
            "relative": {"file": relative},
            "preset": {"preset": "../" + relative},
            "not_pdf": {"file": "notes.txt"},
            "ok": {"file": "lead_magnet_audit.pdf"},
        }.items():
            r = await client.get("/download/lead-magnet", params=query, headers=HEADERS)
            bodies[name] = (r.status, await r.read())
        return bodies

    bodies = _run(fetch)
    for name in ("absolute", "relative", "preset", "not_pdf"):
        status, body = bodies[name]
        assert b"SECRET-OUTSIDE-CAMPAIGN" not in body, name
        assert b"not a lead magnet" not in body, name
    assert bodies["ok"] == (200, b"%PDF-audit")


def test_extension_requires_auth_and_is_not_cached(example_profile):
    CACHED_ZIP.unlink(missing_ok=True)

    async def fetch(client):
        anon = await client.get("/download/extension")
        authed = await client.get("/download/extension", headers=HEADERS)
        return anon.status, await anon.read(), authed.status, authed.headers.get("Content-Disposition"), await authed.read()

    anon_status, anon_body, status, disposition, body = _run(fetch)
    assert anon_status == 401 and not anon_body.startswith(b"PK")
    assert status == 200 and 'filename="serm_cookie_sync_extension.zip"' in disposition
    names = zipfile.ZipFile(io.BytesIO(body)).namelist()
    assert "manifest.json" in names and "background.js" in names
    assert not CACHED_ZIP.exists()


def test_stale_extension_zip_is_removed_at_start(example_profile):
    CACHED_ZIP.write_bytes(b"PK old build with a token inside")
    admin.create_admin_app()
    assert not CACHED_ZIP.exists()


def test_import_session_accepts_only_registry_platforms(example_profile, monkeypatch):
    from config.settings import DATA_DIR, SESSIONS_DIR
    from core.session_manager import SessionManager

    async def no_browser(platform):
        return {"authorized": True, "platform": platform}
    monkeypatch.setattr(SessionManager, "verify_session", staticmethod(no_browser))
    escaped = DATA_DIR / "evil_profile"

    async def post(client):
        out = {}
        for platform in ("../evil", "nope", "vc"):
            r = await client.post("/api/sessions/import", json={"platform": platform, "cookies": "a=1"},
                                  headers=HEADERS)
            out[platform] = r.status
        return out

    statuses = _run(post)
    assert statuses == {"../evil": 400, "nope": 400, "vc": 200}
    assert not escaped.exists()
    assert not (SESSIONS_DIR / "nope_profile").exists()
    assert (SESSIONS_DIR / "vc_profile" / "storage_state.json").is_file()
