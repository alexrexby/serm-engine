"""
VC.ru publishing (plan-autopublish B2, calibrated on prod 2026-10-20): «Написать» opens the editor modal, the body is
pasted as HTML, the autosave POST .../editor carrying it returns the entry id; «Опубликовать» asks
window.confirm("Опубликовать пост?"), then POST editor/<id>/publish and the editor navigates to the post page.
Published only when the publish request answered OK and the post page answers 200.
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import publishers.vc_publisher as vc  # noqa: E402
from core.models import Article  # noqa: E402

SESSION = '{"cookies": [{"name": "osnova-remember", "value": "x", "domain": ".vc.ru", "path": "/"}], "origins": []}'
ENTRY_ID = 1000042
DRAFT_URL = f"https://vc.ru/id1000001/{ENTRY_ID}"  # entry.url of the autosave
POST_URL = f"https://vc.ru/id1000001/{ENTRY_ID}-odin-protiv-studiy-solo-saas"  # where the editor navigates after publishing
CANONICAL_URL = f"https://vc.ru/id1000001/{ENTRY_ID}-solo-saas"  # 301 target, prod 20.10
EDIT_URL = f"https://vc.ru/?modal=editor&action=edit&id={ENTRY_ID}"
BODY_HTML = "<h2>Предыстория</h2>" + "<p>" + "Абзац статьи про соло-разработку. " * 60 + "</p>"
SAVE_URL = "https://api.vc.ru/v2.1/editor"
PUBLISH_URL = f"https://api.vc.ru/v2.1/editor/{ENTRY_ID}/publish"


def blocks(html_parts):
    # block shape of the editor's save payload seen on prod 20.10
    return {"blocks": [{"type": "text", "cover": False, "hidden": False, "anchor": "", "data": {"text": h}}
                       for h in html_parts]}


class Request:
    method = "POST"


class Response:
    def __init__(self, url, status, entry=None, body=None):
        self.url, self.status, self.ok = url, status, 200 <= status < 300
        self.request = Request()
        self._entry, self._body = entry, body

    def json(self):
        if self._body is not None:
            return self._body
        return {"message": "", "result": {"entry": self._entry}}


class ExpectResponse:
    """page.expect_response: on exit picks the first matching response, like Playwright, or raises its TimeoutError."""

    def __init__(self, page, predicate):
        self.page, self.predicate = page, predicate

    def __enter__(self):
        self.start = len(self.page.responses)
        return self

    def __exit__(self, exc_type, *exc):
        if exc_type:
            return False
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        matched = [r for r in self.page.responses[self.start:] if self.predicate(r)]
        if not matched:
            raise PlaywrightTimeoutError("Timeout exceeded while waiting for event \"response\"")
        self.value = matched[0]
        return False


class Dialog:
    def __init__(self, page, message):
        self.page, self.type, self.message = page, "confirm", message

    def accept(self):
        self.page.answers.append(("accept", self.message))

    def dismiss(self):
        self.page.answers.append(("dismiss", self.message))


class Locator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector
        self.first = self.last = self

    def click(self):
        page = self.page
        if "Написать" in self.selector:
            page.clicks.append("write")
            return
        if "Опубликовать" not in self.selector:
            page.clicks.append("title")
            return
        page.clicks.append("publish")
        if page.scenario == "breaks_after_click":
            raise RuntimeError("Target page, context or browser has been closed")
        message = "Пост не сохранен. Точно хотите выйти?" if page.scenario == "other_dialog" else vc.CONFIRM_MESSAGE
        # headless Playwright dismisses a dialog nobody answers: the click then does nothing
        for handler in page.handlers["dialog"]:
            handler(Dialog(page, message))
        if ("accept", vc.CONFIRM_MESSAGE) not in page.answers:
            return
        status = {"publish_rejected": 403, "monthly_limit": 422}.get(page.scenario, 200)
        # the body vc answered on 2026-10-20 to an account without Pro that used up its monthly posts
        body = {"message": "Запрос не может быть выполнен",
                "error": {"code": 422, "info": {"errorCode": "PRO_LIMIT_EXCEED"}}} if status == 422 else None
        page.emit(Response(PUBLISH_URL, status, {"id": ENTRY_ID}, body))
        if status == 200:
            # the server may answer 200 and still not publish (premoderation, hidden post…): only the public page tells
            page.public = page.scenario != "not_public"
            if page.scenario != "stays_in_editor":
                page.url = POST_URL


class Keyboard:
    def press(self, key):
        pass

    def type(self, text):
        pass


class VcPage:
    """vc.ru with the editor modal as calibrated 20.10 (see module docstring). Anonymous GET: a draft → 403,
    a published post → 301 to its canonical address → 200."""

    def __init__(self, scenario):
        # publishes | other_dialog | publish_rejected | stays_in_editor | not_public | title_only_save | save_fails
        # | paste_partial | nothing_saved | no_blocks | late_ok | no_editor | breaks_after_click
        self.scenario = scenario
        self.url = "about:blank"
        self.keyboard = Keyboard()
        self.responses, self.clicks, self.answers, self.public_gets = [], [], [], []
        self.handlers = {"dialog": [], "response": []}
        self.body_text = ""
        self.public = False

    def emit(self, response):
        self.responses.append(response)
        for handler in self.handlers["response"]:
            handler(response)

    def public_page(self, url):
        self.public_gets.append(url)
        return (200, CANONICAL_URL) if self.public else (403, url)

    def goto(self, url, **kwargs):
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def wait_for_selector(self, selector, timeout):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        if self.scenario == "no_editor":
            raise PlaywrightTimeoutError("Timeout exceeded")
        self.url = "https://vc.ru/?modal=editor"

    def wait_for_url(self, pattern, timeout):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        if not pattern.search(self.url):
            raise PlaywrightTimeoutError("Timeout exceeded")

    def on(self, event, handler):
        self.handlers[event].append(handler)

    def expect_response(self, predicate, timeout):
        return ExpectResponse(self, predicate)

    def evaluate(self, script, arg=None):
        if arg is not None:  # the body paste
            self.body_text = arg[:200] if self.scenario == "paste_partial" else arg
            if self.scenario == "nothing_saved":
                self.emit(Response(SAVE_URL, 500))
                return None
            entry = {"id": ENTRY_ID, "url": DRAFT_URL, "is_published": False}
            # the title autosave (debounced) lands after the paste started: blocks hold only an empty paragraph
            self.emit(Response(SAVE_URL, 200, {**entry, "entry": blocks(["<p></p>"])}))
            if self.scenario == "title_only_save":
                return None
            status = 500 if self.scenario in ("save_fails", "late_ok") else 200
            self.url = EDIT_URL
            content = {"version": 1} if self.scenario == "no_blocks" else blocks([self.body_text])
            self.emit(Response(SAVE_URL, status, {**entry, "entry": content}))
            if self.scenario == "late_ok":  # a retried autosave succeeds after the 500
                self.emit(Response(SAVE_URL, 200, {**entry, "entry": blocks([self.body_text])}))
            return None
        # the blocks' text: title block + body blocks
        import re
        return "Заголовок поста" + re.sub(r"<[^>]+>", "", self.body_text)

    def locator(self, selector):
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
    publisher = vc.VCPublisher.__new__(vc.VCPublisher)
    publisher.platform_name = "vc"
    publisher.session_dir = tmp_path

    def run(page):
        context = type("Context", (), {"new_page": lambda self: page})()
        monkeypatch.setattr(vc, "open_context", lambda p, session_dir: (Browser(), context))
        monkeypatch.setattr(vc, "public_page", page.public_page)
        return publisher.publish(Article(id="a1", campaign_id="c", platform="vc", title="Заголовок поста", lead="",
                                         content_markdown="", content_html=BODY_HTML, tags="", status="ready"))
    return run


def test_published_post_gives_canonical_url(publish):
    page = VcPage("publishes")

    res = publish(page)

    assert (res.success, res.status, res.published_url) == (True, "published", CANONICAL_URL)
    assert page.answers == [("accept", vc.CONFIRM_MESSAGE)]
    assert page.public_gets == [POST_URL]


def test_other_dialog_is_dismissed_and_nothing_published(publish):
    page = VcPage("other_dialog")

    res = publish(page)

    assert page.answers == [("dismiss", "Пост не сохранен. Точно хотите выйти?")]
    assert (res.success, res.status, res.published_url) == (True, "draft_saved", EDIT_URL)
    assert "запрос публикации не ушёл" in res.message


def test_rejected_publish_stays_draft(publish):
    res = publish(VcPage("publish_rejected"))

    assert (res.status, res.published_url) == ("draft_saved", EDIT_URL)
    assert "403" in res.message


def test_monthly_limit_is_named_in_the_alert(publish):
    res = publish(VcPage("monthly_limit"))

    assert (res.status, res.published_url) == ("draft_saved", EDIT_URL)
    assert "PRO_LIMIT_EXCEED" in res.message and "лимит" in res.message


def test_post_url_falls_back_to_saved_entry_url(publish):
    # the server took the publish but the editor did not navigate: the autosave's entry url is checked instead
    page = VcPage("stays_in_editor")

    res = publish(page)

    assert (res.status, res.published_url) == ("published", CANONICAL_URL)
    assert page.public_gets == [DRAFT_URL]


def test_post_not_public_is_draft(publish):
    # publish answered 200 but anyone without the author's cookies gets 403: not a published post
    page = VcPage("not_public")

    res = publish(page)

    assert (res.success, res.status, res.published_url) == (True, "draft_saved", EDIT_URL)
    assert "не открывается без входа" in res.message


@pytest.mark.parametrize("scenario", ["title_only_save", "save_fails", "paste_partial"])
def test_unsaved_or_partial_body_is_not_published(publish, scenario):
    # only an OK autosave whose blocks carry the body, and the body in the editor blocks, lets the button be pressed
    page = VcPage(scenario)

    res = publish(page)

    assert "publish" not in page.clicks
    assert (res.success, res.status, res.published_url) == (True, "draft_saved", EDIT_URL)
    assert "«Опубликовать» не нажималась" in res.message


def test_save_answer_without_blocks_is_named(publish):
    # the blocks path comes from vc's bundle: a changed answer format must not read as «the body was not saved»
    page = VcPage("no_blocks")

    res = publish(page)

    assert "publish" not in page.clicks
    assert res.status == "draft_saved"
    assert "формат ответа VC.ru изменился" in res.message


def test_ok_save_after_error_publishes(publish):
    res = publish(VcPage("late_ok"))

    assert (res.status, res.published_url) == ("published", CANONICAL_URL)


def test_nothing_saved_is_an_error_not_a_draft(publish):
    # no draft exists on vc: nothing to link to, nothing clicked — the article may go to the queue again
    page = VcPage("nothing_saved")

    res = publish(page)

    assert "publish" not in page.clicks
    assert not res.success
    assert "сохранение ответило 500" in res.error


def test_editor_not_opening_is_an_error(publish):
    res = publish(VcPage("no_editor"))

    assert not res.success
    assert "Редактор VC.ru не открылся" in res.error


def test_failure_after_click_is_draft_not_error(publish):
    # the click may have published the post: a failed result would put it back in the queue -> a duplicate
    page = VcPage("breaks_after_click")

    res = publish(page)

    assert (res.success, res.status, res.published_url) == (True, "draft_saved", EDIT_URL)
    assert "публикация могла пройти — проверьте по ссылке" in res.message


def test_blocks_chars_reads_saved_blocks_only():
    entry = {"id": ENTRY_ID, "title": "Заголовок " * 50, "entry": blocks(["<p>Один <b>два</b></p>", "<h2>Три</h2>"])}

    assert vc._blocks_chars(entry) == len("Одиндва") + len("Три")
    assert vc._blocks_chars({"id": ENTRY_ID}) == 0


def test_dialog_handler_never_raises():
    class Broken:
        type, message = "confirm", vc.CONFIRM_MESSAGE

        def accept(self):
            raise RuntimeError("Target closed")

    seen = []
    vc.answer_dialog(Broken(), seen)

    assert seen == [vc.CONFIRM_MESSAGE]


def test_confirm_is_dismissed_without_handler_and_accepted_with_it(chromium):
    # root cause of the 20.10 no-op clicks: headless Chromium answers an unhandled confirm with «Отмена»
    from playwright.sync_api import sync_playwright
    html = ('<button id="b" onclick="document.title = confirm(this.dataset.msg) ? \'yes\' : \'no\'" '
            'data-msg="Опубликовать пост?">Опубликовать</button>')
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            page.click("#b")
            assert page.title() == "no"

            seen = []
            page.on("dialog", lambda d: vc.answer_dialog(d, seen))
            page.click("#b")
            assert page.title() == "yes"

            page.evaluate("() => document.getElementById('b').dataset.msg = 'Пост не сохранен. Точно хотите выйти?'")
            page.click("#b")
            assert page.title() == "no"
            assert seen == ["Опубликовать пост?", "Пост не сохранен. Точно хотите выйти?"]
        finally:
            browser.close()


def test_public_page_follows_redirect_and_reports_forbidden():
    # the real urllib path against a local server shaped like vc 20.10: post 301 → canonical 200, draft 403
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/id1/7":
                self.send_response(301)
                self.send_header("Location", "/id1/7-solo-saas")
            elif self.path == "/id1/7-solo-saas":
                self.send_response(200)
            else:
                self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert vc.public_page(base + "/id1/7") == (200, base + "/id1/7-solo-saas")
        assert vc.public_page(base + "/id1/8") == (403, base + "/id1/8")
    finally:
        server.shutdown()
