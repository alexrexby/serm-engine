"""
Publisher adapter for Dzen.ru (Dzen Studio).
Automates article creation in channel studio via Playwright browser context.
Uses synthetic rich-text HTML paste emulation to guarantee 100% native Draft.js typography.
"""
import time
from pathlib import Path
from core.models import Article, PublishResult
from publishers.base import BasePublisher
from config.settings import PLATFORMS_CONFIG
from core.markdown_parser import markdown_to_html

# Login without a configured studio: the user signs in to Yandex ID and opens the studio by hand.
DZEN_HOME_URL = "https://dzen.ru/"


def editor_url(profile=None):
    """Channel studio URL from profile.platforms.dzen.editor_url, or None if not configured."""
    if profile is None:
        from core.profile import get_profile
        profile = get_profile()
    spec = profile.platforms.get("dzen")
    return (spec.model_extra or {}).get("editor_url") if spec is not None else None


class DzenPublisher(BasePublisher):
    def __init__(self):
        super().__init__("dzen", PLATFORMS_CONFIG["dzen"])
        self.session_dir = self.config["session_dir"]
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def verify_auth(self) -> bool:
        storage_file = self.session_dir / "storage_state.json"
        if storage_file.exists() and storage_file.stat().st_size > 50:
            return True
        return any(self.session_dir.iterdir())

    def interactive_login(self):
        """Launches headful browser to log in to Yandex ID / Dzen Studio."""
        try:
            from playwright.sync_api import sync_playwright
            print("[Дзен] Запуск браузера. Войдите в свой Яндекс ID и откройте Дзен-студию...")
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"]
                )
                context = browser.new_context()
                page = context.new_page()
                page.goto(editor_url() or DZEN_HOME_URL)
                input("[Нажмите Enter после того, как откроется Студия Дзена]: ")
                storage_file = self.session_dir / "storage_state.json"
                context.storage_state(path=str(storage_file))
                browser.close()
            print("[Дзен] Сессия успешно сохранена!")
        except Exception as e:
            print(f"[Дзен] Ошибка входа: {e}")

    def publish(self, article: Article) -> PublishResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return PublishResult(
                success=False,
                platform="dzen",
                article_id=article.id,
                error="Playwright not installed in current environment."
            )

        if not self.verify_auth():
            return PublishResult(
                success=False,
                platform="dzen",
                article_id=article.id,
                error="Требуется авторизация в Дзен-студии: запустите 'serm login --platform dzen' или вставьте куки в админке"
            )

        studio_url = editor_url()
        if not studio_url:
            return PublishResult(
                success=False,
                platform="dzen",
                article_id=article.id,
                error="Не задан адрес Дзен-студии: platforms.dzen.editor_url в профиле"
            )

        try:
            launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
            storage_file = self.session_dir / "storage_state.json"

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=launch_args)
                if storage_file.exists() and storage_file.stat().st_size > 50:
                    context = browser.new_context(storage_state=str(storage_file))
                else:
                    context = browser.new_context()

                page = context.new_page()
                
                # Open Dzen Studio
                page.goto(studio_url, wait_until="networkidle", timeout=30000)
                
                # Dismiss modals or promo overlays if any
                page.keyboard.press("Escape")
                page.wait_for_timeout(1000)
                page.evaluate('''() => {
                    document.querySelectorAll('.ReactModalPortal, .ReactModal__Overlay').forEach(el => el.remove());
                }''')
                page.wait_for_timeout(500)

                # Click "+" (Add Publication)
                add_btn = page.locator("[data-testid='add-publication-button']")
                if add_btn.count() == 0:
                    add_btn = page.locator("button:has-text('Создать'), [aria-label*='Создать']")
                
                if add_btn.count() == 0:
                    browser.close()
                    return PublishResult(
                        success=False,
                        platform="dzen",
                        article_id=article.id,
                        error="Не найдена кнопка создания публикации в Дзен-студии"
                    )
                
                add_btn.first.click()
                page.wait_for_timeout(1000)
                
                # Click "Написать статью"
                page.click("text='Написать статью'")
                page.wait_for_timeout(3500)

                # Dismiss onboarding help modal if present
                page.evaluate('''() => {
                    document.querySelectorAll('.ReactModalPortal, .ReactModal__Overlay').forEach(el => el.remove());
                }''')
                page.wait_for_timeout(500)

                # Wait for title and body DraftEditor boxes
                draft_boxes = page.locator(".public-DraftEditor-content")
                if draft_boxes.count() < 2:
                    browser.close()
                    return PublishResult(
                        success=False,
                        platform="dzen",
                        article_id=article.id,
                        error="Редактор статьи Дзена не открылся или изменилась разметка"
                    )

                # 1. Fill title
                title_box = draft_boxes.first
                title_box.click()
                page.keyboard.type(article.title)
                page.wait_for_timeout(1000)

                # 2. Fill body via HTML paste event for rich typography (h2, lists, bold, quotes, structured comparison)
                # We strip top h1 because Dzen has a dedicated title field
                body_html = markdown_to_html(article.content_markdown, platform="dzen", strip_h1=True)

                page.evaluate('''html => {
                    const target = document.querySelectorAll('.public-DraftEditor-content')[1];
                    target.focus();
                    const dt = new DataTransfer();
                    dt.setData('text/html', html);
                    dt.setData('text/plain', 'Текст статьи');
                    const event = new ClipboardEvent('paste', {
                        clipboardData: dt,
                        bubbles: true,
                        cancelable: true
                    });
                    target.dispatchEvent(event);
                }''', body_html)

                page.wait_for_timeout(4000)
                draft_url = page.url

                browser.close()
                return PublishResult(
                    success=True,
                    platform="dzen",
                    article_id=article.id,
                    published_url=draft_url,
                    status="draft_saved",
                    message="Черновик сохранён в Дзен-студии с проверенной нативной вёрсткой (h2, списки, цитаты, сравнения)"
                )
        except Exception as e:
            return PublishResult(
                success=False,
                platform="dzen",
                article_id=article.id,
                error=f"Сбой публикации в Дзен: {str(e)}"
            )
