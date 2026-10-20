"""
Publisher adapter for VC.ru.
Opens the editor modal («Написать»), pastes the body as HTML into Editor.js, waits for the autosave and publishes;
published only when the server answered the publish request and the post page answers 200 (calibrated 2026-10-20).
"""
import logging
import re
import urllib.error
import urllib.request
from html import unescape
from typing import Optional
from core.models import Article, PublishResult
from publishers.base import BasePublisher
from publishers.browser import open_context
from config.settings import PLATFORMS_CONFIG
from core.markdown_parser import markdown_to_html

VC_HOME_URL = "https://vc.ru/"  # /editor redirects to the main page; the editor is the «Написать» modal
WRITE_BUTTON = "button:has-text('Написать')"
TITLE_FIELD = "[data-placeholder='Заголовок'][contenteditable='true']"
PUBLISH_BUTTON = ".editor button:has-text('Опубликовать')"
# the editor asks window.confirm before POST editor/<id>/publish (Editor-CLzS82K9.js); headless dismisses it by default
CONFIRM_MESSAGE = "Опубликовать пост?"
EDITOR_WAIT_MS = 15000
SAVE_WAIT_MS = 20000  # autosave is debounced ~1 s after a change
PUBLISH_WAIT_MS = 20000
POST_PAGE_WAIT_MS = 15000
PUBLIC_CHECK_TIMEOUT_S = 15
SAVE_RE = re.compile(r"api\.vc\.ru/v[\d.]+/editor(?:\?|$)")


def edit_url(entry_id) -> str:
    return f"https://vc.ru/?modal=editor&action=edit&id={entry_id}"


# errorCode of a refused publish → what the owner does about it (texts from vc's editor bundle, 2026-10-20)
REFUSALS = {
    "PRO_LIMIT_EXCEED": "исчерпан месячный лимит публикаций vc без подписки Pro: опубликовать в следующем месяце или купить Pro",
    "POST_TITLE_TOO_LONG": "слишком длинный заголовок",
    "CONTENT_IS_INVALID": "слишком много текста в одном блоке",
    "POST_CONTENT_BLOCKS_LIMIT_EXCEEDED": "слишком много блоков в посте",
    "USER_BANNED": "аккаунт заблокирован на vc",
}


def refusal(response) -> str:
    """«, <errorCode>: <what it means>» from a refused publish's body; empty when the body says nothing."""
    try:
        code = ((response.json().get("error") or {}).get("info") or {}).get("errorCode")
    except Exception:
        return ""
    if not code:
        return ""
    return f", {code}: {REFUSALS[code]}" if code in REFUSALS else f", {code}"


def _plain_chars(html: str) -> int:
    """Non-space characters of the text an HTML fragment renders to."""
    return len(re.sub(r"\s+", "", unescape(re.sub(r"<[^>]+>", "", html))))


logger = logging.getLogger(__name__)


def _is_save(response) -> bool:
    """The editor's autosave: POST .../editor (not .../editor/<id>/publish)."""
    return response.request.method == "POST" and bool(SAVE_RE.search(response.url))


def _blocks_chars(entry: dict) -> int:
    """Non-space text chars in the saved entry's blocks (result.entry.entry.blocks — the editor reloads its blocks
    from there, index-*.js: updateEntryProperty(t,"blocks",i.entry.blocks))."""
    def strings(v):
        if isinstance(v, str):
            yield v
        elif isinstance(v, dict):
            for x in v.values():
                yield from strings(x)
        elif isinstance(v, list):
            for x in v:
                yield from strings(x)
    content = entry.get("entry")
    blocks = content.get("blocks") if isinstance(content, dict) else None
    # only the blocks' data: type/anchor are service fields ({"type": "text", "data": {"text": "<p>…</p>"}})
    return sum(_plain_chars(text) for block in blocks or [] if isinstance(block, dict)
               for text in strings(block.get("data")))


def answer_dialog(dialog, seen: list) -> None:
    """Accepts only the editor's publish confirm; any other browser dialog is dismissed. seen collects the messages.
    Runs in Playwright's event dispatch, so it never raises."""
    try:
        seen.append(dialog.message.strip())
        if dialog.type == "confirm" and dialog.message.strip() == CONFIRM_MESSAGE:
            dialog.accept()
        else:
            dialog.dismiss()
    except Exception:
        logger.exception("vc: не удалось ответить на окно браузера")


def public_page(url: str):
    """(status, final URL) of an anonymous GET, redirects followed. No cookies: the author's own draft answers 403
    to everyone else, a published post 301 → its canonical address → 200 (prod 2026-10-20)."""
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=PUBLIC_CHECK_TIMEOUT_S) as answer:
            return answer.status, answer.geturl()
    except urllib.error.HTTPError as e:
        return e.code, url


def _saved_entry(response) -> dict:
    try:
        entry = response.json()["result"]["entry"]
    except Exception:
        return {}
    return entry if isinstance(entry, dict) else {}


class VCPublisher(BasePublisher):
    def __init__(self):
        super().__init__("vc", PLATFORMS_CONFIG["vc"])
        self.session_dir = self.config["session_dir"]
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def verify_auth(self) -> bool:
        storage_file = self.session_dir / "storage_state.json"
        if storage_file.exists() and storage_file.stat().st_size > 50:
            return True
        return any(self.session_dir.iterdir())

    def interactive_login(self):
        """Launches headful browser for one-time manual login to VC.ru."""
        try:
            from playwright.sync_api import sync_playwright
            print(f"[VC.ru] Запуск окна браузера для авторизации. Войдите в свой аккаунт VC.ru...")
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(self.session_dir),
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"]
                )
                page = context.new_page()
                page.goto("https://vc.ru/auth")
                print("[VC.ru] После завершения входа и появления аватарки закройте браузер или нажмите Enter в консоли.")
                input("[Нажмите Enter после авторизации на VC.ru]: ")
                context.close()
            print("[VC.ru] Сессия успешно сохранена!")
        except Exception as e:
            print(f"[VC.ru] Ошибка интерактивного входа: {e}")

    def publish(self, article: Article) -> PublishResult:
        """Publishes the article to the account's blog on VC.ru."""
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
        except ImportError:
            return PublishResult(
                success=False,
                platform="vc",
                article_id=article.id,
                error="Playwright not installed in current environment."
            )

        if not self.verify_auth():
            return PublishResult(
                success=False,
                platform="vc",
                article_id=article.id,
                error="Требуется однократная авторизация: запустите 'serm login --platform vc' или вставьте куки в админке"
            )

        try:
            with sync_playwright() as p:
                browser, context = open_context(p, self.session_dir)
                try:
                    page = context.new_page()
                    page.goto(VC_HOME_URL, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3000)
                    page.locator(WRITE_BUTTON).first.click()
                    try:
                        page.wait_for_selector(TITLE_FIELD, timeout=EDITOR_WAIT_MS)
                    except PlaywrightTimeoutError:
                        return PublishResult(
                            success=False,
                            platform="vc",
                            article_id=article.id,
                            error="Редактор VC.ru не открылся по «Написать» — сессия устарела? Обновите куки в админке"
                        )

                    page.locator(TITLE_FIELD).first.click()
                    page.keyboard.type(article.title)
                    page.keyboard.press("Enter")  # focus moves to the first body block
                    page.wait_for_timeout(1000)

                    body_html = article.content_html or markdown_to_html(article.content_markdown, platform="vc", strip_h1=True)
                    body_chars = _plain_chars(body_html)
                    saves = []
                    page.on("response", lambda r: saves.append(r) if _is_save(r) else None)
                    page.evaluate('''html => {
                        const target = document.activeElement;
                        const dt = new DataTransfer();
                        dt.setData('text/html', html);
                        dt.setData('text/plain', 'Текст статьи');
                        target.dispatchEvent(new ClipboardEvent('paste', {
                            clipboardData: dt,
                            bubbles: true,
                            cancelable: true
                        }));
                    }''', body_html)
                    entry, save_error = self._wait_body_save(page, saves, body_chars)
                    return self._publish_draft(page, article, entry, save_error, body_chars)
                finally:
                    browser.close()
        except Exception as e:
            return PublishResult(
                success=False,
                platform="vc",
                article_id=article.id,
                error=f"Сбой публикации VC.ru: {str(e)}"
            )

    @staticmethod
    def _wait_body_save(page, saves: list, body_chars: int):
        """(entry, save_error): the first OK autosave whose blocks carry ≥80% of the body; else the last saved entry
        (for its draft link, may be {}) and why the body is not confirmed."""
        seen, last, failed, no_blocks = 0, {}, None, False
        waited = 0
        while True:
            for response in saves[seen:]:
                seen += 1
                if not response.ok:
                    failed = response.status
                    continue
                failed = None  # a later OK answer supersedes an earlier error
                entry = _saved_entry(response)
                last = entry or last
                content = entry.get("entry")
                no_blocks = not (isinstance(content, dict) and isinstance(content.get("blocks"), list))
                if _blocks_chars(entry) >= 0.8 * body_chars:
                    return entry, None
            if waited >= SAVE_WAIT_MS:
                break
            page.wait_for_timeout(500)
            waited += 500
        if failed:
            return last, f"сохранение ответило {failed}"
        if no_blocks:
            # «could not read» must not look like «empty»: the blocks path was taken from vc's bundle
            return last, "в ответе сохранения нет result.entry.entry.blocks — формат ответа VC.ru изменился?"
        return last, f"сохранения с текстом не было за {SAVE_WAIT_MS // 1000} с"

    def _publish_draft(self, page, article: Article, entry: dict, save_error: Optional[str], body_chars: int) -> PublishResult:
        """Publishes the saved draft. entry: the autosave's result.entry (id, url); save_error: why the autosave with
        the body is not confirmed, or None; body_chars: non-space chars of the body."""
        entry_id = entry.get("id")
        if not entry_id:
            # no draft on the server, nothing was clicked: a failure, safe to retry
            return PublishResult(
                success=False,
                platform="vc",
                article_id=article.id,
                error=f"VC.ru не сохранил черновик ({save_error or 'нет id в ответе сохранения'}) — «Опубликовать» не нажималась"
            )
        address = edit_url(entry_id)

        def draft(message):
            return PublishResult(
                success=True,
                platform="vc",
                article_id=article.id,
                published_url=address,
                status="draft_saved",
                message=message
            )

        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        not_published = "Черновик на VC.ru, не опубликован: "
        not_clicked = " — «Опубликовать» не нажималась"
        if save_error:
            return draft(not_published + f"текст не сохранился на сервере ({save_error})" + not_clicked)
        text = page.evaluate("() => [...document.querySelectorAll('.ce-block')].map(b => b.innerText).join('')")
        got = len(re.sub(r"\s+", "", text)) - len(re.sub(r"\s+", "", article.title))
        if got < 0.8 * body_chars:
            return draft(not_published + f"текст вставился не полностью ({got} из {body_chars} симв.)" + not_clicked)

        dialogs = []
        publish_re = re.compile(rf"api\.vc\.ru/v[\d.]+/editor/{entry_id}/publish")
        # from the click on the post may be public: any failure is a draft to check, never an error to retry
        try:
            page.on("dialog", lambda dialog: answer_dialog(dialog, dialogs))
            try:
                with page.expect_response(lambda r: r.request.method == "POST" and bool(publish_re.search(r.url)),
                                          timeout=PUBLISH_WAIT_MS) as published:
                    page.locator(PUBLISH_BUTTON).last.click()
            except PlaywrightTimeoutError:
                shown = f"окно «{dialogs[0]}»" if dialogs else "окно подтверждения не появилось"
                return draft(f"Клик «Опубликовать» на VC.ru: запрос публикации не ушёл за {PUBLISH_WAIT_MS // 1000} с "
                             f"({shown}) — откройте ссылку: если пост уже вышел, ничего не делайте")
            if not published.value.ok:
                return draft(f"VC.ru отклонил публикацию (ответ {published.value.status}{refusal(published.value)})"
                             " — черновик по ссылке")
            # after publishing the editor navigates to the post page
            try:
                page.wait_for_url(re.compile(rf"vc\.ru/[^?#]+/{entry_id}(?![0-9])"), timeout=POST_PAGE_WAIT_MS)
                post_url = page.url.split("?")[0].split("#")[0]
            except PlaywrightTimeoutError:
                post_url = entry.get("url") or ""
            # anonymous: a draft is 403 to anyone but its author; the post's canonical address is what we store
            answer, final_url = public_page(post_url) if post_url.startswith("https://vc.ru/") else (None, post_url)
            if answer != 200 or not re.search(rf"vc\.ru/[^?#]+/{entry_id}(?:-|$)", final_url):
                return draft(f"VC.ru принял публикацию, но пост не открывается без входа ({post_url or 'нет адреса'}: "
                             f"{answer}) — проверьте по ссылке")
            post_url = final_url
        except Exception as e:
            return draft(f"Сбой после клика «Опубликовать» на VC.ru ({e}): публикация могла пройти — проверьте по ссылке")
        return PublishResult(
            success=True,
            platform="vc",
            article_id=article.id,
            published_url=post_url,
            status="published",
            message="Пост опубликован на VC.ru (сервер принял публикацию, страница поста открывается)"
        )
