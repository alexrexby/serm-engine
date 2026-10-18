"""
Platform Session & Cookie Manager for Headless Server Environments.
Supports importing cookies / storage states from Web UI, validating credentials,
and checking live authorization status across VC.ru, Spark.ru, Dzen.ru and E-xecutive.
"""
import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

from config.settings import SESSIONS_DIR, PLATFORMS_CONFIG

logger = logging.getLogger("SessionManager")


class SessionManager:
    """Manages platform session states on headless servers."""

    @staticmethod
    def get_profile_dir(platform: str) -> Path:
        dir_path = SESSIONS_DIR / f"{platform}_profile"
        dir_path.mkdir(parents=True, exist_ok=True)
        return dir_path

    @staticmethod
    def get_storage_state_file(platform: str) -> Path:
        return SessionManager.get_profile_dir(platform) / "storage_state.json"

    @staticmethod
    async def import_cookies(platform: str, cookie_input: str) -> Dict[str, Any]:
        """
        Accepts raw cookie string ('name=val; name2=val2') or JSON cookies array.
        Saves directly to Playwright storage_state.json format.
        """
        profile_dir = SessionManager.get_profile_dir(platform)
        storage_file = SessionManager.get_storage_state_file(platform)

        cookies = []
        domain_defaults = {
            "vc": ".vc.ru",
            "spark": ".spark.ru",
            "dzen": ".dzen.ru",
            "executive": ".e-xecutive.ru"
        }
        domain = domain_defaults.get(platform, ".vc.ru")

        # Parse JSON if provided
        cookie_input = cookie_input.strip()
        if cookie_input.startswith("[") and cookie_input.endswith("]"):
            try:
                raw_cookies = json.loads(cookie_input)
                for c in raw_cookies:
                    cookies.append({
                        "name": c.get("name"),
                        "value": c.get("value"),
                        "domain": c.get("domain", domain),
                        "path": c.get("path", "/"),
                        "httpOnly": c.get("httpOnly", False),
                        "secure": c.get("secure", True),
                        "sameSite": c.get("sameSite", "Lax")
                    })
            except Exception as e:
                return {"success": False, "error": f"JSON parse error: {e}"}
        else:
            # Parse header string 'name=val; name2=val2'
            for item in cookie_input.split(";"):
                if "=" in item:
                    k, v = item.strip().split("=", 1)
                    cookies.append({
                        "name": k.strip(),
                        "value": v.strip(),
                        "domain": domain,
                        "path": "/",
                        "httpOnly": False,
                        "secure": True,
                        "sameSite": "Lax"
                    })

        if not cookies:
            return {"success": False, "error": "No valid cookies found in input."}

        storage_data = {
            "cookies": cookies,
            "origins": []
        }
        storage_file.write_text(json.dumps(storage_data, indent=2), encoding="utf-8")
        logger.info(f"Imported {len(cookies)} cookies for {platform} into {storage_file}")

        # Immediately verify the session
        return await SessionManager.verify_session(platform)

    @staticmethod
    async def verify_session(platform: str) -> Dict[str, Any]:
        """Runs a headless browser check using saved cookies to verify authorization."""
        storage_file = SessionManager.get_storage_state_file(platform)
        if not storage_file.exists():
            return {
                "authorized": False,
                "platform": platform,
                "message": "Сессия не загружена. Вставьте cookies площадки."
            }

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
            )
            try:
                context = await browser.new_context(
                    storage_state=str(storage_file),
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
                page = await context.new_page()

                if platform == "vc":
                    await page.goto("https://vc.ru/settings", timeout=25000, wait_until="domcontentloaded")
                    content = await page.content()
                    if "/auth" in page.url or ("войти" in content.lower() and "настройки" not in content.lower()):
                        return {"authorized": False, "platform": platform, "message": "Сессия VC.ru истекла или недействительна."}
                    
                    # Try to extract username / name
                    title = await page.title()
                    return {"authorized": True, "platform": platform, "message": f"Авторизован на VC.ru ({title})"}

                elif platform == "spark":
                    await page.goto("https://spark.ru/profile", timeout=25000, wait_until="domcontentloaded")
                    content = await page.content()
                    if "/login" in page.url:
                        return {"authorized": False, "platform": platform, "message": "Сессия Spark.ru истекла."}
                    return {"authorized": True, "platform": platform, "message": "Авторизован на Spark.ru"}

                elif platform == "dzen":
                    await page.goto("https://dzen.ru/profile/editor", timeout=25000, wait_until="domcontentloaded")
                    if "passport.yandex.ru" in page.url:
                        return {"authorized": False, "platform": platform, "message": "Сессия Дзена требует авторизации в Яндекс ID."}
                    return {"authorized": True, "platform": platform, "message": "Авторизован в Дзен-Студии"}

                elif platform == "executive":
                    await page.goto("https://www.e-xecutive.ru/author", timeout=25000, wait_until="domcontentloaded")
                    if "login" in page.url:
                        return {"authorized": False, "platform": platform, "message": "Сессия E-xecutive.ru не авторизована."}
                    return {"authorized": True, "platform": platform, "message": "Авторизован в кабинете автора E-xecutive"}

                return {"authorized": True, "platform": platform, "message": "Сессия сохранена"}

            except Exception as e:
                logger.warning(f"Error checking session for {platform}: {e}")
                return {"authorized": False, "platform": platform, "message": f"Ошибка проверки сессии: {str(e)[:100]}"}
            finally:
                await browser.close()

    @staticmethod
    async def export_storage_state(platform: str) -> Optional[Path]:
        """
        Saves cookies of the persistent browser profile (written by `cli.py login`) to
        storage_state.json — the single file `cli.py push-sessions` delivers to the server.
        Returns None when the directory holds no Chromium profile (dzen login writes
        storage_state.json itself; an empty profile must not overwrite it).
        """
        profile_dir = SessionManager.get_profile_dir(platform)
        storage_file = SessionManager.get_storage_state_file(platform)
        if not (profile_dir / "Default").is_dir():
            return None
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(user_data_dir=str(profile_dir), headless=True)
            try:
                await context.storage_state(path=str(storage_file))
            finally:
                await context.close()
        os.chmod(storage_file, 0o600)
        logger.info(f"Exported storage state for {platform} into {storage_file}")
        return storage_file
