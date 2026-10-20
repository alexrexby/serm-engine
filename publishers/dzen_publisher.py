"""
Publisher adapter for Dzen.ru (Dzen Studio).
Automates article creation in channel studio via Playwright browser context.
Uses synthetic rich-text HTML paste emulation to guarantee 100% native Draft.js typography.
"""
import re
import time
import base64
from html import unescape
from pathlib import Path
from typing import Optional
from core.models import Article, PublishResult
from publishers.base import BasePublisher
from publishers.browser import open_context
from config.settings import PLATFORMS_CONFIG
from core.markdown_parser import markdown_to_html

# Login without a configured studio: the user signs in to Yandex ID and opens the studio by hand.
DZEN_HOME_URL = "https://dzen.ru/"
# Editor header status: «Сохранено около …» -> «Опубликовано около …»; the class carries a hash suffix
STATUS_SELECTOR = "[class*='editor-header__status']"
PUBLISH_BUTTON = "[data-testid='article-publish-btn']"  # stays on the page after publishing: not a signal
# the click opens the «Публикация» settings modal (no role=dialog, not a ReactModalPortal; prod 2026-10-19);
# its submit is the real publish
CONFIRM_BUTTON = "[data-testid='publish-btn']"
SAVE_WAIT_MS = 20000  # the autosave POST goes ~4.5 s after the paste
SAVED_STATUS_WAIT_MS = 10000
PUBLISH_WAIT_MS = 30000
PUBLISH_RECHECK_MS = 15000
RELOAD_WAIT_MS = 10000
EDIT_ID_RE = re.compile(r"/([0-9a-f]+)/edit(?:[/?#]|$)")


def public_url(edit_url: str) -> Optional[str]:
    """https://dzen.ru/a/<base64url(publication id bytes)>, the id being the last hex segment of the edit URL."""
    match = EDIT_ID_RE.search(edit_url or "")
    if not match:
        return None
    try:
        raw = bytes.fromhex(match.group(1))
    except ValueError:
        return None
    return "https://dzen.ru/a/" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _status(page) -> str:
    status = page.locator(STATUS_SELECTOR)
    return status.first.inner_text().strip() if status.count() else ""


def _plain_chars(html: str) -> int:
    """Non-space characters of the text an HTML fragment renders to."""
    return len(re.sub(r"\s+", "", unescape(re.sub(r"<[^>]+>", "", html))))


def _is_content_save(response) -> bool:
    """The editor's autosave: title and body go in one POST update-publication-content (calibrated 19.10)."""
    return "update-publication-content" in response.url and response.request.method == "POST"


def _wait_status(page, prefix: str, timeout_ms: int, on_tick=None) -> bool:
    """Polls the editor status until it starts with prefix; on_tick runs between polls."""
    waited = 0
    while True:
        if _status(page).startswith(prefix):
            return True
        if waited >= timeout_ms:
            return False
        if on_tick:
            on_tick()
        page.wait_for_timeout(500)
        waited += 500


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
            from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
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
            with sync_playwright() as p:
                browser, context = open_context(p, self.session_dir)
                try:
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
                    save_error = None
                    try:
                        with page.expect_response(_is_content_save, timeout=SAVE_WAIT_MS) as saved:
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
                        if not saved.value.ok:
                            save_error = f"сохранение ответило {saved.value.status}"
                    except PlaywrightTimeoutError:
                        save_error = f"запрос сохранения не пришёл за {SAVE_WAIT_MS // 1000} с"

                    return self._publish_draft(page, article, page.url, save_error, _plain_chars(body_html))
                finally:
                    browser.close()
        except Exception as e:
            return PublishResult(
                success=False,
                platform="dzen",
                article_id=article.id,
                error=f"Сбой публикации в Дзен: {str(e)}"
            )

    def _publish_draft(self, page, article: Article, edit_url: str, save_error: Optional[str], body_chars: int) -> PublishResult:
        """Publishes the filled draft; published only when the editor status says «Опубликовано».
        save_error: why the autosave after the paste is not confirmed, or None; body_chars: non-space chars of the body."""
        def draft(message):
            return PublishResult(
                success=True,
                platform="dzen",
                article_id=article.id,
                published_url=edit_url,
                status="draft_saved",
                message=message
            )

        not_published = "Черновик в Дзен-студии, не опубликован: "
        not_clicked = " — «Опубликовать» не нажималась"
        address = public_url(edit_url)
        if not address:
            return draft(not_published + f"в адресе редактора нет id публикации ({edit_url})" + not_clicked)
        if save_error:
            return draft(not_published + f"тело не сохранилось на сервере ({save_error})" + not_clicked)
        body = page.locator(".public-DraftEditor-content").nth(1).inner_text()
        got = len(re.sub(r"\s+", "", body))
        if got < 0.8 * body_chars:
            return draft(not_published + f"тело вставилось не полностью ({got} из {body_chars} симв.)" + not_clicked)
        # secondary: «Идёт сохранение» -> «Сохранено меньше минуты назад»
        if not _wait_status(page, "Сохранено", SAVED_STATUS_WAIT_MS):
            return draft(not_published + f"статус редактора не «Сохранено» (сейчас: «{_status(page)}»)" + not_clicked)

        # from the click on the article may be public: any failure is a draft to check, never an error to retry
        try:
            page.locator(PUBLISH_BUTTON).first.click()
            confirmed = []

            def confirm_dialog():
                # a settings/confirm dialog may appear after the click; its button is pressed once
                if not confirmed and page.locator(CONFIRM_BUTTON).count() > 0:
                    page.locator(CONFIRM_BUTTON).first.click()
                    confirmed.append(True)

            if not _wait_status(page, "Опубликовано", PUBLISH_WAIT_MS, on_tick=confirm_dialog):
                # a late status change still means a public article: one more look before reporting a draft
                page.wait_for_timeout(PUBLISH_RECHECK_MS)
                # after the «Публикация» modal the header status disappears until a reload (prod 2026-10-19)
                if not _status(page).startswith("Опубликовано"):
                    page.goto(edit_url, wait_until="networkidle", timeout=30000)
                    _wait_status(page, "Опубликовано", RELOAD_WAIT_MS)
                if not _status(page).startswith("Опубликовано"):
                    dialog = " (диалог подтверждения нажат)" if confirmed else ""
                    seconds = (PUBLISH_WAIT_MS + PUBLISH_RECHECK_MS) // 1000
                    return draft(f"Клик «Опубликовать» в Дзене{dialog}: статус не подтвердился за {seconds} с "
                                 f"(сейчас: «{_status(page)}») — откройте ссылку: если статья уже вышла, ничего не делайте")
        except Exception as e:
            return draft(f"Сбой после клика «Опубликовать» в Дзене ({e}): публикация могла пройти — проверьте по ссылке")
        return PublishResult(
            success=True,
            platform="dzen",
            article_id=article.id,
            published_url=address,
            status="published",
            message="Статья опубликована в Дзене (статус редактора «Опубликовано»)"
        )
