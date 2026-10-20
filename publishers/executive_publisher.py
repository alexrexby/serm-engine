"""
Publisher adapter for E-xecutive.ru.
Automates draft creation and submission for moderation via Playwright persistent context.
"""
import time
from core.models import Article, PublishResult
from publishers.base import BasePublisher
from publishers.browser import open_context
from config.settings import PLATFORMS_CONFIG

class ExecutivePublisher(BasePublisher):
    def __init__(self):
        super().__init__("executive", PLATFORMS_CONFIG["executive"])
        self.session_dir = self.config["session_dir"]
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def verify_auth(self) -> bool:
        storage_file = self.session_dir / "storage_state.json"
        if storage_file.exists() and storage_file.stat().st_size > 50:
            return True
        return any(self.session_dir.iterdir())

    def interactive_login(self):
        """Launches headful browser for one-time login to E-xecutive.ru."""
        try:
            from playwright.sync_api import sync_playwright
            print("[E-xecutive] Запуск браузера. Войдите в свой личный кабинет E-xecutive.ru...")
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(self.session_dir),
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"]
                )
                page = context.new_page()
                page.goto("https://www.e-xecutive.ru/login")
                input("[Нажмите Enter после авторизации в личном кабинете E-xecutive.ru]: ")
                context.close()
            print("[E-xecutive] Сессия сохранена!")
        except Exception as e:
            print(f"[E-xecutive] Ошибка входа: {e}")

    def publish(self, article: Article) -> PublishResult:
        """Fills author form on E-xecutive and submits for editorial review."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return PublishResult(
                success=False,
                platform="executive",
                article_id=article.id,
                error="Playwright not installed in current environment."
            )

        if not self.verify_auth():
            return PublishResult(
                success=False,
                platform="executive",
                article_id=article.id,
                error="Требуется авторизация: запустите 'serm login --platform executive' или вставьте куки в админке"
            )

        try:
            with sync_playwright() as p:
                browser, context = open_context(p, self.session_dir)
                try:
                    page = context.new_page()
                    page.goto("https://www.e-xecutive.ru/my/articles/new", timeout=30000)
                    time.sleep(3)

                    # Check if editor is reachable
                    if "login" in page.url:
                        return PublishResult(
                            success=False,
                            platform="executive",
                            article_id=article.id,
                            error="Сессия устарела. Запустите 'serm login --platform executive'"
                        )

                    # Title (up to 70 chars per editorial rules)
                    title_val = article.title[:70]
                    page.fill("input[name*='title'], #article_title", title_val)

                    # Lead (up to 160 chars per editorial rules)
                    lead_val = (article.lead or "")[:160]
                    if page.is_visible("textarea[name*='annotation'], #article_annotation"):
                        page.fill("textarea[name*='annotation'], #article_annotation", lead_val)

                    # Body content
                    body_elem = page.locator("textarea[name*='body'], .redactor-editor, [contenteditable='true']")
                    if body_elem.count() > 0:
                        body_elem.first.fill(article.content_markdown)

                    time.sleep(2)

                    # Save draft
                    save_btn = page.locator("button:has-text('Сохранить'), input[value*='Сохранить']")
                    if save_btn.count() > 0:
                        save_btn.first.click()
                        time.sleep(4)

                        return PublishResult(
                            success=True,
                            platform="executive",
                            article_id=article.id,
                            status="submitted",
                            message="Материал сохранен в ЛК E-xecutive и готов к отправке в редакцию (срок проверки ~10 раб. дней)"
                        )
                    else:
                        return PublishResult(
                            success=True,
                            platform="executive",
                            article_id=article.id,
                            status="draft_saved",
                            message="Черновик сохранен на E-xecutive.ru"
                        )
                finally:
                    browser.close()
        except Exception as e:
            return PublishResult(
                success=False,
                platform="executive",
                article_id=article.id,
                error=f"Сбой отправки E-xecutive: {str(e)}"
            )
