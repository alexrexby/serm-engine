"""
Administrative Control Panel for SERM Engine & Secrets Management.
Protected by Token-based Authentication & Session Cookies.

Security invariants:
1. No unauthenticated access to secrets, status, or downloads (returns 401 / Login Screen).
2. Supports both Direct Token Magic-link (?token=...) and Web Password Login.
3. Cryptographically signed session cookies with HMAC.
4. Auto-generates high-entropy ADMIN_TOKEN if none exists in .env (the value is never logged).
5. Secret keys from the secrets form are written only with an explicit change flag from the UI
   (`__set__<KEY>`) and a non-empty value; /api/status returns masks, not secret values.

Pages are rendered from templates/admin/* (profile override templates first, see core/templating.py);
brand values come from the profile on the server side.
"""
import io
import os
import re
import sys
import json
import hmac
import hashlib
import secrets
import asyncio
import logging
import zipfile
from pathlib import Path
from typing import Dict, Any, Optional, List

from aiohttp import web, ClientSession
from config.settings import DATA_DIR, EXPORTS_DIR, SESSIONS_DIR, BASE_DIR, ENV_FILE, PLATFORMS_CONFIG, load_settings
from core import content, envfile
from core.db import Database
from core.indexer import FastIndexer, indexable_url
from core.horizon_connector import HorizonConnector
from core.profile import get_profile
from core.session_manager import SessionManager
from core.templating import render
from publishers.registry import PLATFORMS, enabled as enabled_platforms, get_publisher


logger = logging.getLogger("AdminPanel")

ENV_PATH = ENV_FILE  # SERM_ENV_FILE or <repo>/.env: the same file Settings and cli read
COOKIE_NAME = "serm_admin_session"
# Link to the source code in the admin footer (AGPL-3.0 §13)
SOURCE_CODE_URL = "https://github.com/alexrexby/serm-engine"
SET_FLAG_PREFIX = "__set__"
SECRET_KEY_MARKERS = ("TOKEN", "PASSWORD", "SECRET", "API_KEY")
EXTENSION_DIR = BASE_DIR / "extension"
# Zip cached by older versions (could carry a hardcoded admin token); now built in memory per request
STALE_EXTENSION_ZIP = EXPORTS_DIR / "serm_cookie_sync_extension.zip"
# Lead rows in /api/analytics (read-only view of what the owner marked in Telegram)
LEAD_FIELDS = ("id", "user_id", "username", "first_name", "slug", "status", "created_at", "status_at",
               "product", "amount")
# Bot restarts after saving secrets: the loop keeps only a weak reference to a task
_RESTART_TASKS: set = set()
# Indexer pings after a single-article publish run in the background: same weak-reference rule
_INDEX_TASKS: set = set()


def _restart_done(task: asyncio.Task) -> None:
    _RESTART_TASKS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("Bot restart after saving secrets failed", exc_info=task.exception())


def _index_done(task: asyncio.Task) -> None:
    _INDEX_TASKS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("Indexer ping after publishing failed", exc_info=task.exception())


def republish_error(article) -> Optional[str]:
    """Why an article must not be published from the admin, or None. Each publish creates a new copy on the
    platform: an article already there (published or left as a draft) would get a duplicate.
    An editorial pitch is a local package, so it may be prepared again."""
    spec = PLATFORMS.get(article.platform)
    if article.status in ("ready", "failed") or (spec is not None and spec.type == "editorial_pitch"):
        return None
    if article.status == "published":
        return f"Статья уже опубликована: {article.published_url or 'адрес не сохранён'}"
    if article.status == "draft_saved":
        return (f"Статья уже лежит на площадке черновиком: {article.published_url or 'адрес не сохранён'} — "
                "допубликуйте её в кабинете площадки, повторная публикация создаст дубль")
    return f"Статья в статусе «{article.status}»: публикуются только статьи в статусе ready или failed"


def get_or_create_admin_token() -> str:
    """Retrieves or creates a secure admin token from .env."""
    token = os.getenv("ADMIN_TOKEN") or os.getenv("ADMIN_PASSWORD")
    if token:
        return token

    # Check .env file directly
    env = envfile.read(ENV_PATH)
    val = env.get("ADMIN_TOKEN") or env.get("ADMIN_PASSWORD")
    if val:
        os.environ["ADMIN_TOKEN"] = val
        return val

    # Generate a strong 32-character random token
    new_token = secrets.token_hex(16)
    os.environ["ADMIN_TOKEN"] = new_token
    envfile.write(ENV_PATH, {"ADMIN_TOKEN": new_token})
    logger.info(f"Generated new ADMIN_TOKEN and saved it to {ENV_PATH}")
    return new_token


SIGNING_KEY = hashlib.sha256(get_or_create_admin_token().encode()).digest()


def sign_token(token: str) -> str:
    sig = hmac.new(SIGNING_KEY, token.encode(), hashlib.sha256).hexdigest()
    return f"{token}:{sig}"


def verify_signed_token(cookie_val: Optional[str]) -> bool:
    if not cookie_val or ":" not in cookie_val:
        return False
    token, sig = cookie_val.split(":", 1)
    admin_token = get_or_create_admin_token()
    if not hmac.compare_digest(token, admin_token):
        return False
    expected_sig = hmac.new(SIGNING_KEY, token.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected_sig)


def is_authenticated(request: web.Request) -> bool:
    admin_token = get_or_create_admin_token()
    
    # 1. Check Query parameter (?token=...)
    q_token = request.query.get("token")
    if q_token and hmac.compare_digest(q_token.strip(), admin_token):
        return True

    # 2. Check HTTP Header (X-Admin-Token or Authorization: Bearer ...)
    h_token = request.headers.get("X-Admin-Token")
    if h_token and hmac.compare_digest(h_token.strip(), admin_token):
        return True
    
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        b_token = auth_header.split(" ", 1)[1].strip()
        if hmac.compare_digest(b_token, admin_token):
            return True

    # 3. Check Signed Session Cookie
    cookie_val = request.cookies.get(COOKIE_NAME)
    return verify_signed_token(cookie_val)


def is_secret_key(key: str) -> bool:
    return any(marker in key.upper() for marker in SECRET_KEY_MARKERS)


def mask_secret(value: str) -> str:
    """Hint for a stored secret in /api/status: '' when unset, never the value itself."""
    if not value:
        return ""
    return "••••" + value[-4:] if len(value) >= 12 else "••••"


def secrets_form_updates(data: Dict[str, Any]) -> Dict[str, str]:
    """
    Keys from the secrets form payload that may be written to .env.
    A secret key is applied only with its change flag `__set__<KEY>` (set by the UI after an
    explicit "Изменить") and a non-empty value: browser autofill or a failed /api/status can
    no longer blank or replace a stored token. Other keys are applied as sent.
    """
    updates = {}
    for key, value in data.items():
        if key.startswith(SET_FLAG_PREFIX) or value is None:
            continue
        if is_secret_key(key):
            if str(data.get(SET_FLAG_PREFIX + key, "")).lower() not in ("1", "true", "on"):
                continue
            value = str(value).strip()
            if not value:
                continue
        updates[key] = str(value)
    return updates


def active_campaign_id() -> str:
    """SERM_ACTIVE_CAMPAIGN (env, then .env; read fresh), else the profile campaign."""
    campaign = load_settings().active_campaign
    return campaign if campaign is not None else get_profile().campaign_id


def lead_magnet_file(camp_dir: Path, name: str) -> Optional[Path]:
    """A lead magnet PDF of the campaign by file name; absolute paths, `..` and other files give None."""
    base = camp_dir.resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base) or not (target.name.startswith("lead_magnet_") and target.suffix == ".pdf"):
        return None
    return target if target.is_file() else None


def preset_slug(value: Any) -> str:
    """Lead magnet preset from a request body as a file-name-safe slug ([a-z0-9_-])."""
    return re.sub(r"[^a-z0-9_-]", "_", str(value).strip().lower())[:64]


def session_platform(value: Any) -> Optional[str]:
    """Platform key for a browser session: only registry platforms with a session, path inside SESSIONS_DIR."""
    key = str(value or "").strip().lower()
    if key not in PLATFORMS or not PLATFORMS[key].has_session:
        return None
    base = SESSIONS_DIR.resolve()
    return key if (base / f"{key}_profile").resolve().is_relative_to(base) else None


def extension_zip() -> bytes:
    """The cookie sync extension packed in memory (nothing is written to data/)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(EXTENSION_DIR.rglob("*")):
            if file.is_file():
                zf.write(file, file.relative_to(EXTENSION_DIR))
    return buf.getvalue()


def remove_stale_extension_zip() -> None:
    if STALE_EXTENSION_ZIP.exists():
        STALE_EXTENSION_ZIP.unlink()
        logger.warning("Removed cached extension zip %s (built by an older version)", STALE_EXTENSION_ZIP)


def form_defaults(env: Dict[str, str]) -> Dict[str, str]:
    """Channel/contact shown in the secrets form: .env value, else the profile."""
    telegram = get_profile().telegram
    return {
        "channel": env.get("OPP_CHANNEL", telegram.channel),
        "contact": env.get("OPP_CONTACT", telegram.contact),
    }


def render_text(name: str, **ctx: Any) -> str:
    """One-line text template (admin/*.txt.j2) without the trailing newline."""
    return render(name, get_profile(), **ctx).strip()


def render_page(name: str) -> str:
    """Admin page with brand values rendered on the server (no /api/profile for JS)."""
    profile = get_profile()
    return render(
        f"admin/{name}",
        profile,
        campaign_id=active_campaign_id(),
        fast_index_title=render_text("admin/fast_index_title.txt.j2"),
        source_url=SOURCE_CODE_URL,
        **form_defaults(SecretsManager.read_env()),
    )


class SecretsManager:
    """
    .env persistence for the admin panel: a thin wrapper over core.envfile.
    Keys are updated in place; unknown keys, comments and order are kept; a backup is made
    before every write; values with line breaks are rejected (ValueError).
    """

    @staticmethod
    def read_env() -> Dict[str, str]:
        return envfile.read(ENV_PATH)

    @staticmethod
    def write_env(new_secrets: Dict[str, Any]):
        written = envfile.write(ENV_PATH, new_secrets)
        for k, v in written.items():
            os.environ[k] = v


class AdminController:
    """Aiohttp Request Handlers with strict token/session authentication."""

    def __init__(self, bot_supervisor=None):
        self.bot_supervisor = bot_supervisor
        from core.autopilot import AutopilotEngine
        self.autopilot = AutopilotEngine()

    async def handle_login(self, request: web.Request) -> web.Response:
        data = await request.json()
        token = data.get("token", "").strip()
        admin_token = get_or_create_admin_token()

        if hmac.compare_digest(token, admin_token):
            response = web.json_response({"success": True})
            response.set_cookie(
                COOKIE_NAME,
                sign_token(admin_token),
                httponly=True,
                samesite="Strict",
                max_age=2592000,  # 30 days
                path="/"
            )
            return response
        return web.json_response({"success": False, "error": "Invalid token"}, status=401)

    async def handle_logout(self, request: web.Request) -> web.Response:
        response = web.json_response({"success": True})
        response.del_cookie(COOKIE_NAME, path="/")
        return response

    async def handle_index(self, request: web.Request) -> web.Response:
        admin_token = get_or_create_admin_token()
        q_token = request.query.get("token")

        if q_token and hmac.compare_digest(q_token.strip(), admin_token):
            response = web.HTTPFound("/admin")
            response.set_cookie(
                COOKIE_NAME,
                sign_token(admin_token),
                httponly=True,
                samesite="Strict",
                max_age=2592000,
                path="/"
            )
            return response

        if not is_authenticated(request):
            return web.Response(text=render_page("login.html.j2"), content_type="text/html")

        return web.Response(text=render_page("index.html.j2"), content_type="text/html")


    async def handle_autopilot_status(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        return web.json_response(self.autopilot.get_status())

    async def handle_autopilot_toggle(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        data = await request.json() if request.can_read_body else {}
        enabled = data.get("enabled")
        if enabled is None:
            enabled = not self.autopilot.is_enabled()
        self.autopilot.set_enabled(bool(enabled))
        return web.json_response({"success": True, "status": self.autopilot.get_status()})

    async def handle_autopilot_run_now(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        res_art = await self.autopilot.step_article_publication()
        res_tg = await self.autopilot.step_telegram_post()
        return web.json_response({
            "success": True,
            "article": res_art,
            "telegram": res_tg,
            "status": self.autopilot.get_status()
        })

    async def handle_status(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        env = SecretsManager.read_env()
        defaults = form_defaults(env)
        bot_online = False
        bot_username = ""
        
        if self.bot_supervisor and self.bot_supervisor.is_running:
            bot_online = True
            bot_username = self.bot_supervisor.bot_username or "bot"

        return web.json_response({
            "bot_online": bot_online,
            "bot_username": bot_username,
            "has_token": bool(env.get("OPP_BOT_TOKEN")),
            "opp_bot_token": mask_secret(env.get("OPP_BOT_TOKEN", "")),
            "opp_channel": defaults["channel"],
            "opp_contact": defaults["contact"],
            "autopilot": self.autopilot.get_status(),
            "serm_tg_token": mask_secret(env.get("SERM_TG_BOT_TOKEN", "")),
            "serm_tg_channel": env.get("SERM_TG_CHANNEL_ID", ""),
            "serm_vk_token": mask_secret(env.get("SERM_VK_TOKEN", ""))
        })

    async def handle_check_bot(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        data = await request.json()
        # Empty token from a locked form field: test the stored one
        token = data.get("token", "").strip() or SecretsManager.read_env().get("OPP_BOT_TOKEN", "")
        if not token:
            return web.json_response({"ok": False, "description": "Token is empty"})

        async with ClientSession() as session:
            try:
                async with session.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10) as resp:
                    res = await resp.json()
                    return web.json_response(res)
            except Exception as e:
                return web.json_response({"ok": False, "description": str(e)})

    async def handle_save_secrets(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        data = await request.json()
        try:
            SecretsManager.write_env(secrets_form_updates(data))
        except ValueError as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)
        logger.info("Secrets updated via Admin Panel.")

        if self.bot_supervisor:
            task = asyncio.create_task(self.bot_supervisor.restart())
            _RESTART_TASKS.add(task)
            task.add_done_callback(_restart_done)

        return web.json_response({"success": True})

    async def handle_horizon_news(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        force = request.query.get("refresh", "0") == "1"
        news = HorizonConnector.get_latest_news(force_refresh=force)
        return web.json_response({"news": news})

    async def handle_horizon_synthesize(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        data = await request.json()
        news_item = dict(data.get("news_item") or {})
        if news_item.get("lead_magnet_preset"):
            news_item["lead_magnet_preset"] = preset_slug(news_item["lead_magnet_preset"])
        profile = get_profile()
        # Article generation is synchronous (LLM over urllib, minutes): a worker thread keeps the loop free
        res = await asyncio.to_thread(HorizonConnector.prepare_campaign_from_news, news_item, profile=profile)
        if not Path(res["exports_dir"]).resolve().is_relative_to(EXPORTS_DIR.resolve()):
            return web.json_response({"success": False, "error": "Campaign directory outside exports"}, status=500)
        HorizonConnector.start_lead_magnet(res, profile)
        return web.json_response({"success": True, "result": res})

    async def handle_list_lead_magnets(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        campaign_id = active_campaign_id()
        camp_dir = EXPORTS_DIR / f"campaign_{campaign_id}"
        camp_dir.mkdir(parents=True, exist_ok=True)

        profile = get_profile()
        magnets = []
        for p in sorted(camp_dir.glob("lead_magnet_*.pdf")):
            magnet = next((lm for lm in profile.lead_magnets if lm.file_prefix == p.name), None)
            name = render_text(
                "admin/lead_magnet_name.txt.j2",
                filename=p.name,
                clean_name=p.stem.replace("lead_magnet_", "").replace("_", " ").capitalize(),
                magnet=magnet,
            )

            magnets.append({
                "filename": p.name,
                "name": name,
                "size_kb": round(p.stat().st_size / 1024, 1),
                "url": f"/download/lead-magnet?file={p.name}"
            })

        return web.json_response({"magnets": magnets})

    async def handle_generate_lead_magnet(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        data = await request.json()
        topic = data.get("topic", "general_audit").strip()
        campaign_id = active_campaign_id()
        camp_dir = EXPORTS_DIR / f"campaign_{campaign_id}"
        camp_dir.mkdir(parents=True, exist_ok=True)

        res = await content.lead_magnet(topic, get_profile(), output_dir=camp_dir)

        return web.json_response({
            "success": True,
            "title": res["title"],
            "slug": res["slug"],
            "filename": res["pdf"].name,
            "size_kb": round(res["size_bytes"] / 1024, 1),
            "download_url": f"/download/lead-magnet?file={res['pdf'].name}"
        })

    async def handle_download_lead_magnet(self, request: web.Request) -> web.StreamResponse:
        if not is_authenticated(request):
            return web.Response(text="Unauthorized", status=401)

        file_param = request.query.get("file")
        preset_param = request.query.get("preset")
        campaign_id = active_campaign_id()
        camp_dir = EXPORTS_DIR / f"campaign_{campaign_id}"

        pdf_path = None
        if file_param:
            pdf_path = lead_magnet_file(camp_dir, file_param)
        elif preset_param:
            pdf_path = lead_magnet_file(camp_dir, f"lead_magnet_{preset_param}.pdf")

        if not pdf_path or not pdf_path.exists():
            pdf_path = camp_dir / "lead_magnet_15_business_holes.pdf"

        if not pdf_path.exists():
            for p in EXPORTS_DIR.glob("**/lead_magnet_*.pdf"):
                pdf_path = p
                break

        if pdf_path and pdf_path.exists():
            return web.FileResponse(
                path=pdf_path,
                headers={"Content-Disposition": f'attachment; filename="{pdf_path.name}"'}
            )
        return web.Response(text="Lead magnet PDF not found", status=404)

    async def handle_download_extension(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.Response(text="Unauthorized", status=401)

        if not EXTENSION_DIR.is_dir():
            return web.Response(text="Extension zip archive not found", status=404)
        return web.Response(
            body=extension_zip(),
            content_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="serm_cookie_sync_extension.zip"'}
        )

    async def handle_sessions_status(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        platforms = [key for key in enabled_platforms(get_profile()) if PLATFORMS[key].has_session]
        results = {}
        for plat in platforms:
            storage = SessionManager.get_storage_state_file(plat)
            exists = storage.exists() and storage.stat().st_size > 50
            results[plat] = {
                "has_cookies": exists,
                "storage_file": str(storage)
            }
        return web.json_response({"platforms": results})

    async def handle_import_session(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        data = await request.json()
        platform = data.get("platform", "").strip().lower()
        cookies = data.get("cookies", "").strip()
        if not platform or not cookies:
            return web.json_response({"authorized": False, "message": "Площадка и куки обязательны"})
        if session_platform(platform) is None:
            return web.json_response({"authorized": False, "message": "Неизвестная площадка"}, status=400)

        res = await SessionManager.import_cookies(platform, cookies)
        return web.json_response(res)

    async def handle_list_articles(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        campaign_id = request.query.get("campaign") or active_campaign_id()
        db = Database()
        all_articles = db.list_articles_by_campaign(campaign_id)
        
        # Deduplicate to show the latest article per platform
        plat_articles = {}
        for a in reversed(all_articles):
            if a.platform not in plat_articles:
                plat_articles[a.platform] = a

        order = list(PLATFORMS)
        sorted_articles = []
        for p in order:
            if p in plat_articles:
                sorted_articles.append(plat_articles[p])
        for p, a in plat_articles.items():
            if p not in order:
                sorted_articles.append(a)

        result_list = []
        for a in sorted_articles:
            plat_meta = PLATFORMS_CONFIG.get(a.platform, {})
            result_list.append({
                "id": a.id,
                "campaign_id": a.campaign_id,
                "platform": a.platform,
                "platform_name": plat_meta.get("name", a.platform.upper()),
                "platform_url": plat_meta.get("url", ""),
                "platform_type": plat_meta.get("type", "direct_ugc"),
                "title": a.title,
                "lead": a.lead or "",
                "status": a.status,
                "published_url": a.published_url,
                "published_at": a.published_at,
                "error_message": a.error_message
            })

        return web.json_response({
            "success": True,
            "campaign_id": campaign_id,
            "articles": result_list
        })

    async def handle_get_article_content(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        article_id = request.query.get("id")
        if not article_id:
            return web.json_response({"success": False, "error": "ID статьи обязателен"}, status=400)

        db = Database()
        article = db.get_article(article_id)
        if not article:
            return web.json_response({"success": False, "error": "Статья не найдена"}, status=404)

        plat_meta = PLATFORMS_CONFIG.get(article.platform, {})

        # Formulate pitch text for editorial platforms
        pitch_text = None
        if article.platform in ("tbank", "biz360", "rb"):
            pitch_text = render(f"pitch/{article.platform}.txt.j2", get_profile(), article=article)

        return web.json_response({
            "success": True,
            "article": {
                "id": article.id,
                "campaign_id": article.campaign_id,
                "platform": article.platform,
                "platform_name": plat_meta.get("name", article.platform.upper()),
                "title": article.title,
                "lead": article.lead or "",
                "content_markdown": article.content_markdown,
                "content_html": article.content_html or "",
                "tags": article.tags,
                "status": article.status,
                "published_url": article.published_url,
                "published_at": article.published_at,
                "error_message": article.error_message
            },
            "pitch_text": pitch_text
        })

    async def handle_approve_article(self, request: web.Request) -> web.Response:
        """draft -> ready: an LLM article reviewed by the owner becomes publishable."""
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        data = await request.json()
        article_id = data.get("article_id")
        if not article_id:
            return web.json_response({"success": False, "error": "ID статьи не передан"}, status=400)

        db = Database()
        article = db.get_article(article_id)
        if not article:
            return web.json_response({"success": False, "error": "Статья не найдена"}, status=404)
        if article.status != "draft":
            return web.json_response(
                {"success": False, "error": f"Одобрить можно только черновик (статус: {article.status})"}, status=409)

        db.update_article_status(article_id=article.id, status="ready")
        logger.info("Article %s approved (draft -> ready)", article.id)
        return web.json_response({"success": True, "status": "ready"})

    async def handle_publish_article(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        data = await request.json()
        article_id = data.get("article_id")
        if not article_id:
            return web.json_response({"success": False, "error": "ID статьи не передан"}, status=400)

        db = Database()
        article = db.get_article(article_id)
        if not article:
            return web.json_response({"success": False, "error": "Статья не найдена"}, status=404)
        if article.status == "draft":
            return web.json_response({"success": False, "error": "Черновик: сначала одобрите статью"}, status=409)
        blocked = republish_error(article)
        if blocked:
            return web.json_response({"success": False, "error": blocked}, status=409)

        try:
            publisher = get_publisher(article.platform)
            # Run Playwright publish in worker thread
            result = await asyncio.to_thread(publisher.publish, article)

            if result.success:
                db.update_article_status(
                    article_id=article.id,
                    status=result.status,
                    published_url=result.published_url
                )
                public_url = indexable_url(result)
                if public_url:
                    indexer = FastIndexer()
                    task = asyncio.create_task(asyncio.to_thread(
                        indexer.ping_all, article.id, article.title, public_url
                    ))
                    _INDEX_TASKS.add(task)
                    task.add_done_callback(_index_done)

                return web.json_response({
                    "success": True,
                    "status": result.status,
                    "published_url": result.published_url,
                    "message": result.message
                })
            else:
                db.update_article_status(
                    article_id=article.id,
                    status="failed",
                    error_message=result.error
                )
                return web.json_response({
                    "success": False,
                    "status": "failed",
                    "error": result.error or "Ошибка публикации"
                })
        except Exception as e:
            logger.exception("Error publishing article %s", article_id)
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def handle_publish_all_ugc(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        data = await request.json() if request.can_read_body else {}
        campaign_id = data.get("campaign_id") or active_campaign_id()
        db = Database()
        all_articles = db.list_articles_by_campaign(campaign_id)
        
        ugc_platforms = [key for key in enabled_platforms(get_profile()) if PLATFORMS[key].type != "editorial_pitch"]
        target_articles = {}
        for a in reversed(all_articles):
            if a.platform in ugc_platforms and a.platform not in target_articles:
                target_articles[a.platform] = a
        # drafts (LLM, review_required) are published only after approval
        target_articles = {p: a for p, a in target_articles.items() if a.status != "draft"}

        results = []
        for plat, article in target_articles.items():
            blocked = republish_error(article)
            if blocked:
                results.append({"platform": plat, "success": False, "status": article.status, "error": blocked})
                continue
            try:
                publisher = get_publisher(plat)
                res = await asyncio.to_thread(publisher.publish, article)
                if res.success:
                    db.update_article_status(
                        article_id=article.id,
                        status=res.status,
                        published_url=res.published_url
                    )
                    public_url = indexable_url(res)
                    if public_url:
                        indexer = FastIndexer()
                        await asyncio.to_thread(indexer.ping_all, article.id, article.title, public_url)
                else:
                    db.update_article_status(article_id=article.id, status="failed", error_message=res.error)

                results.append({
                    "platform": plat,
                    "success": res.success,
                    "published_url": res.published_url,
                    "status": res.status,
                    "error": res.error
                })
            except Exception as e:
                results.append({
                    "platform": plat,
                    "success": False,
                    "error": str(e)
                })

        return web.json_response({
            "success": True,
            "campaign_id": campaign_id,
            "results": results
        })

    async def handle_trigger_fast_index(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        data = await request.json()
        url = data.get("url")
        title = data.get("title") or render_text("admin/fast_index_title.txt.j2")
        if not url:
            return web.json_response({"success": False, "message": "URL обязателен"}, status=400)

        indexer = FastIndexer()
        res = await asyncio.to_thread(indexer.ping_all, "manual", title, url)
        
        summary = {ch: {"success": r.success, "message": r.message} for ch, r in res.items()}
        return web.json_response({
            "success": any(r.success for r in res.values()),
            "results": summary
        })

    async def handle_analytics(self, request: web.Request) -> web.Response:
        if not is_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        db = Database()
        summary = db.get_funnel_summary()
        breakdown = db.get_slug_breakdown()
        recommendations = db.get_content_strategy_recommendations()
        recent_events = db.get_recent_events(limit=50)
        leads = [{key: lead[key] for key in LEAD_FIELDS} for lead in db.list_leads(50)]

        return web.json_response({
            "success": True,
            "summary": summary,
            "breakdown": breakdown,
            "recommendations": recommendations,
            "recent_events": recent_events,
            "leads": leads
        })


def create_admin_app(bot_supervisor=None) -> web.Application:
    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            return web.Response(
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Admin-Token",
                    "Access-Control-Max-Age": "86400"
                }
            )
        resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Admin-Token"
        return resp

    remove_stale_extension_zip()
    app = web.Application(middlewares=[cors_middleware])
    controller = AdminController(bot_supervisor)

    app.router.add_get("/", controller.handle_index)
    app.router.add_get("/admin", controller.handle_index)
    app.router.add_post("/api/login", controller.handle_login)
    app.router.add_post("/api/logout", controller.handle_logout)
    app.router.add_get("/api/status", controller.handle_status)
    app.router.add_get("/api/autopilot/status", controller.handle_autopilot_status)
    app.router.add_post("/api/autopilot/toggle", controller.handle_autopilot_toggle)
    app.router.add_post("/api/autopilot/run-now", controller.handle_autopilot_run_now)
    app.router.add_post("/api/check-bot", controller.handle_check_bot)
    app.router.add_post("/api/secrets", controller.handle_save_secrets)
    
    # Analytics routes
    app.router.add_get("/api/analytics", controller.handle_analytics)

    # Articles & Publishing routes
    app.router.add_get("/api/articles/list", controller.handle_list_articles)
    app.router.add_get("/api/articles/content", controller.handle_get_article_content)
    app.router.add_post("/api/articles/approve", controller.handle_approve_article)
    app.router.add_post("/api/publish/article", controller.handle_publish_article)
    app.router.add_post("/api/publish/campaign", controller.handle_publish_all_ugc)
    app.router.add_post("/api/fast-index/ping", controller.handle_trigger_fast_index)

    # Horizon routes
    app.router.add_get("/api/horizon/news", controller.handle_horizon_news)
    app.router.add_post("/api/horizon/synthesize", controller.handle_horizon_synthesize)
    
    # Lead magnet routes
    app.router.add_get("/api/lead-magnet/list", controller.handle_list_lead_magnets)
    app.router.add_post("/api/lead-magnet/generate", controller.handle_generate_lead_magnet)
    app.router.add_get("/download/lead-magnet", controller.handle_download_lead_magnet)
    app.router.add_get("/download/extension", controller.handle_download_extension)

    # Session routes
    app.router.add_get("/api/sessions/status", controller.handle_sessions_status)
    app.router.add_post("/api/sessions/import", controller.handle_import_session)

    return app

