"""
Browser publishers open their context through publishers/browser.py: the saved session goes to
Browser.new_context(storage_state=...). Regression: vc/spark/executive passed storage_state to
launch_persistent_context, which has no such parameter (TypeError on every run with a session file).
The fake Playwright binds calls to the real Playwright signatures, so a wrong kwarg fails like in prod.
"""
import inspect
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from playwright.sync_api import Browser, BrowserType  # noqa: E402

from core.models import Article  # noqa: E402

SESSION = '{"cookies": [{"name": "sid", "value": "x", "domain": ".example.ru", "path": "/"}], "origins": []}'


class StopAtGoto(Exception):
    pass


def _bind(method, *args, **kwargs):
    inspect.signature(method).bind(None, *args, **kwargs)  # TypeError on a kwarg the real API lacks


class FakePage:
    def goto(self, *args, **kwargs):
        raise StopAtGoto("goto reached")


class FakeContext:
    def new_page(self):
        return FakePage()

    def close(self):
        pass


class FakeBrowser:
    def __init__(self, calls):
        self.calls = calls

    def new_context(self, **kwargs):
        _bind(Browser.new_context, **kwargs)
        self.calls.append(("new_context", kwargs))
        return FakeContext()

    def close(self):
        self.calls.append(("browser.close", {}))


class FakeChromium:
    def __init__(self, calls):
        self.calls = calls

    def launch(self, **kwargs):
        _bind(BrowserType.launch, **kwargs)
        self.calls.append(("launch", kwargs))
        return FakeBrowser(self.calls)

    def launch_persistent_context(self, user_data_dir, **kwargs):
        _bind(BrowserType.launch_persistent_context, user_data_dir, **kwargs)
        self.calls.append(("launch_persistent_context", kwargs))
        return FakeContext()


class FakePlaywright:
    def __init__(self, calls):
        self.chromium = FakeChromium(calls)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def calls(monkeypatch):
    import playwright.sync_api
    recorded = []
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: FakePlaywright(recorded))
    return recorded


def _article(platform):
    return Article(id=f"art-{platform}", campaign_id="c", platform=platform, title="T", lead="",
                   content_markdown="body", content_html="<p>body</p>", tags="", status="ready")


def test_fake_matches_real_playwright_signatures():
    # The guard the fake relies on: storage_state is a new_context parameter, not a persistent-context one
    assert "storage_state" in inspect.signature(Browser.new_context).parameters
    assert "storage_state" not in inspect.signature(BrowserType.launch_persistent_context).parameters


def test_open_context_passes_session_to_new_context(calls, tmp_path):
    from publishers.browser import open_context
    (tmp_path / "storage_state.json").write_text(SESSION, encoding="utf-8")

    browser, context = open_context(FakePlaywright(calls), tmp_path)

    assert [c[0] for c in calls] == ["launch", "new_context"]
    assert calls[0][1]["headless"] is True
    assert calls[1][1] == {"storage_state": str(tmp_path / "storage_state.json")}


def test_open_context_without_session_file(calls, tmp_path):
    from publishers.browser import open_context
    (tmp_path / "storage_state.json").write_text("{}", encoding="utf-8")  # <= 50 bytes: no saved session

    open_context(FakePlaywright(calls), tmp_path)

    assert calls[1] == ("new_context", {})


@pytest.mark.parametrize("module, cls, platform", [
    ("publishers.vc_publisher", "VCPublisher", "vc"),
    ("publishers.spark_publisher", "SparkPublisher", "spark"),
    ("publishers.executive_publisher", "ExecutivePublisher", "executive"),
    ("publishers.dzen_publisher", "DzenPublisher", "dzen"),
])
def test_publisher_reaches_page_with_saved_session(monkeypatch, calls, tmp_path, module, cls, platform):
    import importlib
    mod = importlib.import_module(module)
    publisher = getattr(mod, cls).__new__(getattr(mod, cls))
    publisher.platform_name = platform
    publisher.session_dir = tmp_path
    (tmp_path / "storage_state.json").write_text(SESSION, encoding="utf-8")
    if platform == "dzen":
        monkeypatch.setattr(mod, "editor_url", lambda profile=None: "https://dzen.ru/profile/editor/x")

    res = publisher.publish(_article(platform))

    assert not res.success
    assert "goto reached" in res.error, res.error  # got as far as opening the page, no TypeError
    assert ("new_context", {"storage_state": str(tmp_path / "storage_state.json")}) in calls
    assert calls[-1][0] == "browser.close"


def test_open_context_closes_browser_when_context_fails(calls, tmp_path):
    from publishers.browser import open_context
    (tmp_path / "storage_state.json").write_text("{" + " " * 60, encoding="utf-8")  # truncated by a partial write

    class BrokenBrowser(FakeBrowser):
        def new_context(self, **kwargs):
            raise ValueError("bad storage_state")

    pw = FakePlaywright(calls)
    pw.chromium.launch = lambda **kwargs: BrokenBrowser(calls)

    with pytest.raises(ValueError):
        open_context(pw, tmp_path)
    assert calls == [("browser.close", {})]


class SparkPage:
    """Editor page of spark.ru: the click on «Опубликовать» navigates away only when `lands_on` is set."""

    def __init__(self, lands_on):
        self.url = "https://spark.ru/write"
        self.lands_on = lands_on
        self.selectors = []

    def goto(self, url, **kwargs):
        self.url = url

    def wait_for_selector(self, *args, **kwargs):
        pass

    def fill(self, *args):
        pass

    def evaluate(self, *args):
        pass

    def locator(self, selector):
        self.selectors.append(selector)

        class Button:
            first = None

            def count(self):
                return 1

            def click(self):
                pass

        button = Button()
        button.first = button
        return button

    def wait_for_url(self, predicate, timeout):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        if self.lands_on is None or not predicate(self.lands_on):
            raise PlaywrightTimeoutError(f"Timeout {timeout}ms exceeded.")
        self.url = self.lands_on


@pytest.fixture
def spark(monkeypatch, calls, tmp_path):
    import publishers.spark_publisher as mod
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    (tmp_path / "storage_state.json").write_text(SESSION, encoding="utf-8")
    publisher = mod.SparkPublisher.__new__(mod.SparkPublisher)
    publisher.platform_name = "spark"
    publisher.session_dir = tmp_path

    def run(page):
        class Context(FakeContext):
            def new_page(self):
                return page
        monkeypatch.setattr(mod, "open_context", lambda p, session_dir: (FakeBrowser(calls), Context()))
        return publisher.publish(_article("spark"))
    return run


def test_spark_published_only_after_leaving_editor(spark):
    page = SparkPage(lands_on="https://spark.ru/startup/x/blog/123")

    res = spark(page)

    assert (res.success, res.status, res.published_url) == (True, "published", "https://spark.ru/startup/x/blog/123")
    assert all("input[type='submit']" not in s for s in page.selectors)  # first submit in DOM may be a search form


def test_spark_unconfirmed_click_stays_draft(spark):
    page = SparkPage(lands_on=None)

    res = spark(page)

    assert (res.success, res.status, res.published_url) == (True, "draft_saved", "https://spark.ru/write")
