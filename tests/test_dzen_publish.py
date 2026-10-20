"""
Dzen publishing (plan-autopublish B1): after the body is pasted and autosaved the publisher clicks
«Опубликовать» (and a confirm dialog's button if one shows up) and trusts only the editor status
«Опубликовано…». The public address is computed from the publication id in the edit URL.
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import publishers.dzen_publisher as dzen  # noqa: E402
from core.models import Article  # noqa: E402

EDIT_URL = "https://dzen.ru/profile/editor/id/5f0c1a2b3c4d5e6f7a8b9c0d/6ad5c744be108a145222e11b/edit"
SESSION = '{"cookies": [{"name": "sid", "value": "x", "domain": ".dzen.ru", "path": "/"}], "origins": []}'


def test_public_url_from_edit_url():
    # Pair from the studio: the owner's article published by hand on 19.10
    assert dzen.public_url(EDIT_URL) == "https://dzen.ru/a/atXHRL4QihRSIuEb"
    assert dzen.public_url("https://dzen.ru/profile/editor/id/5f0c1a2b3c4d5e6f7a8b9c0d") is None


class Locator:
    def __init__(self, page, selector, index=0):
        self.page, self.selector, self.index = page, selector, index
        self.first = self

    def nth(self, index):
        return Locator(self.page, self.selector, index)

    def _kind(self):
        s = self.selector
        if "'publish-btn'" in s:
            return "confirm"
        if "article-publish-btn" in s:
            return "publish"
        if "editor-header__status" in s:
            return "status"
        if "DraftEditor" in s:
            return "editor"
        return "other"

    def count(self):
        kind = self._kind()
        if kind == "confirm":
            return 1 if self.page.dialog_open else 0
        return 2 if kind == "editor" else 1

    def click(self):
        kind = self._kind()
        self.page.clicks.append(kind)
        if kind == "publish":
            self.page.clicked_at = self.page.elapsed
            if self.page.scenario in ("dialog", "status_hidden"):
                self.page.dialog_open = True
            elif self.page.scenario == "publishes":
                self.page.status = PUBLISHED
        elif kind == "confirm":
            self.page.dialog_open = False
            self.page.published_on_server = True
            # prod 19.10: after the modal the header status is gone until the editor is reloaded
            self.page.status = "" if self.page.scenario == "status_hidden" else PUBLISHED

    def inner_text(self):
        if self._kind() == "editor":
            return self.page.body_text if self.index == 1 else "T"
        return self.page.status


class Keyboard:
    def press(self, key):
        pass

    def type(self, text):
        pass


SAVED = "Сохранено меньше минуты назад"
SAVING = "Идёт сохранение"
PUBLISHED = "Опубликовано около минуты назад"
SAVE_URL = "https://dzen.ru/editor-api/v2/update-publication-content?publisherId=5f0c1a2b3c4d5e6f7a8b9c0d"


class Response:
    def __init__(self, url, ok):
        self.url, self.ok, self.status = url, ok, 200 if ok else 500
        self.request = type("Request", (), {"method": "POST"})()


class ExpectResponse:
    """page.expect_response: on exit waits for a matching response, like Playwright, or raises its TimeoutError."""

    def __init__(self, page, predicate, timeout):
        self.page, self.predicate, self.timeout = page, predicate, timeout

    def __enter__(self):
        self.page.responses = []
        return self

    def __exit__(self, exc_type, *exc):
        if exc_type:
            return False
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        self.page.wait_for_timeout(self.page.save_delay)
        matched = [r for r in self.page.responses if self.predicate(r)]
        if not matched:
            raise PlaywrightTimeoutError(f"Timeout {self.timeout}ms exceeded while waiting for event \"response\"")
        self.value = matched[0]
        return False


class DzenPage:
    """Studio editor as calibrated 19.10: status «Сохранено меньше минуты назад» before the paste, «Идёт сохранение»
    right after it, ~4.5 s later one POST update-publication-content (title + body) and «Сохранено…» again.
    Time runs in wait_for_timeout."""

    def __init__(self, scenario):
        # publishes | dialog | stays | late | paste_ignored | save_fails | no_save | breaks_after_click
        self.scenario = scenario
        self.url = "about:blank"
        self.status = SAVED
        self.body_text = ""
        self.dialog_open = False
        self.published_on_server = False
        self.clicks = []
        self.keyboard = Keyboard()
        self.elapsed = 0
        self.saving_until = None
        self.clicked_at = None
        self.responses = []
        self.save_delay = 4500

    def goto(self, url, **kwargs):
        self.url = url
        if self.published_on_server:
            self.status = PUBLISHED

    def wait_for_timeout(self, ms):
        self.elapsed += ms
        if self.saving_until is not None and self.elapsed >= self.saving_until:
            self.saving_until = None
            if self.scenario != "no_save":
                self.status = SAVED
                self.responses.append(Response(SAVE_URL, ok=self.scenario != "save_fails"))
        if self.scenario == "late" and self.clicked_at is not None and self.elapsed - self.clicked_at > 35000:
            self.status = PUBLISHED  # published after the 30 s wait

    def expect_response(self, predicate, timeout):
        return ExpectResponse(self, predicate, timeout)

    def evaluate(self, script, arg=None):
        if arg is not None:  # the body paste; the typed title is saved by the same request anyway
            if self.scenario != "paste_ignored":
                self.body_text = "body"
            self.status, self.saving_until = SAVING, self.elapsed + 4500

    def click(self, selector):
        if "Написать статью" in selector:
            self.url = EDIT_URL

    def locator(self, selector):
        if self.scenario == "breaks_after_click" and self.clicked_at is not None:
            raise RuntimeError("Target page, context or browser has been closed")
        return Locator(self, selector)


@pytest.fixture
def publish(monkeypatch, tmp_path):
    import playwright.sync_api

    class Playwright:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Browser:
        def close(self):
            pass

    (tmp_path / "storage_state.json").write_text(SESSION, encoding="utf-8")
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: Playwright())
    monkeypatch.setattr(dzen, "editor_url", lambda profile=None: "https://dzen.ru/profile/editor/id/x")
    publisher = dzen.DzenPublisher.__new__(dzen.DzenPublisher)
    publisher.platform_name = "dzen"
    publisher.session_dir = tmp_path

    def run(page):
        context = type("Context", (), {"new_page": lambda self: page})()
        monkeypatch.setattr(dzen, "open_context", lambda p, session_dir: (Browser(), context))
        return publisher.publish(Article(id="a1", campaign_id="c", platform="dzen", title="T", lead="",
                                         content_markdown="body", content_html="", tags="", status="ready"))
    return run


def test_published_status_gives_public_url(publish):
    page = DzenPage("publishes")

    res = publish(page)

    assert (res.success, res.status, res.published_url) == (True, "published", "https://dzen.ru/a/atXHRL4QihRSIuEb")
    assert page.clicks.count("publish") == 1


def test_confirm_dialog_is_clicked(publish):
    page = DzenPage("dialog")

    res = publish(page)

    assert page.clicks.count("confirm") == 1
    assert res.status == "published"


def test_unchanged_status_stays_draft(publish):
    page = DzenPage("stays")

    res = publish(page)

    assert (res.success, res.status, res.published_url) == (True, "draft_saved", EDIT_URL)
    assert page.clicks.count("publish") == 1
    assert "откройте ссылку: если статья уже вышла, ничего не делайте" in res.message


def test_status_published_late_is_rechecked(publish):
    page = DzenPage("late")

    res = publish(page)

    assert (res.status, res.published_url) == ("published", "https://dzen.ru/a/atXHRL4QihRSIuEb")


def test_status_hidden_after_modal_is_read_after_reload(publish):
    page = DzenPage("status_hidden")

    res = publish(page)

    assert page.clicks.count("confirm") == 1
    assert (res.status, res.published_url) == ("published", "https://dzen.ru/a/atXHRL4QihRSIuEb")
    assert page.url == EDIT_URL


def test_ignored_paste_is_not_published(publish):
    # «Сохранено» is on from the title autosave, the body never arrived: the button must not be pressed
    page = DzenPage("paste_ignored")

    res = publish(page)

    assert page.clicks.count("publish") == 0
    assert (res.status, res.published_url) == ("draft_saved", EDIT_URL)


def test_confirm_button_is_the_settings_modal_submit(chromium):
    # DOM shape seen on prod 2026-10-19 after clicking the header «Опубликовать»
    from playwright.sync_api import sync_playwright
    html = ('<div class="editor-header"><button data-testid="article-publish-btn">Опубликовать</button></div>'
            '<div class="article-editor-desktop--modal__modal-1l"><h2>Публикация</h2>'
            '<label>Опубликовать позже<input type="checkbox"></label>'
            '<div class="modal-actions"><button type="submit" data-testid="publish-btn">Опубликовать</button></div></div>')
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            confirm = page.locator(dzen.CONFIRM_BUTTON)
            assert confirm.count() == 1
            assert confirm.first.get_attribute("type") == "submit"
        finally:
            browser.close()


@pytest.mark.parametrize("scenario", ["save_fails", "no_save"])
def test_unsaved_body_is_not_published(publish, scenario):
    # the body is saved only when update-publication-content answered OK after the paste
    page = DzenPage(scenario)

    res = publish(page)

    assert page.clicks.count("publish") == 0
    assert (res.status, res.published_url) == ("draft_saved", EDIT_URL)


def test_failure_after_click_is_draft_not_error(publish):
    # the click may have published the article: a failed result would put it back in the queue -> a duplicate
    page = DzenPage("breaks_after_click")

    res = publish(page)

    assert page.clicks.count("publish") == 1
    assert (res.success, res.status, res.published_url) == (True, "draft_saved", EDIT_URL)
    assert "публикация могла пройти — проверьте по ссылке" in res.message
