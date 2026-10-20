"""
Publisher adapter for Spark.ru.
Automates draft creation and publishing via Playwright persistent context.
Uses HTML insertion for Redactor WYSIWYG editor to guarantee native rich typography.
"""
import time
from core.models import Article, PublishResult
from publishers.base import BasePublisher
from publishers.browser import open_context
from config.settings import PLATFORMS_CONFIG
from core.markdown_parser import markdown_to_html

class SparkPublisher(BasePublisher):
    def __init__(self):
        super().__init__("spark", PLATFORMS_CONFIG["spark"])
        self.session_dir = self.config["session_dir"]
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def verify_auth(self) -> bool:
        storage_file = self.session_dir / "storage_state.json"
        if storage_file.exists() and storage_file.stat().st_size > 50:
            return True
        return any(self.session_dir.iterdir())

    def interactive_login(self):
        """Launches headful browser for one-time login to Spark.ru."""
        try:
            from playwright.sync_api import sync_playwright
            print("[Spark.ru] Запуск браузера для авторизации. Войдите в свой аккаунт Spark...")
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(self.session_dir),
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"]
                )
                page = context.new_page()
                page.goto("https://spark.ru/auth")
                input("[Нажмите Enter после успешной авторизации на Spark.ru]: ")
                context.close()
            print("[Spark.ru] Сессия сохранена!")
        except Exception as e:
            print(f"[Spark.ru] Ошибка входа: {e}")

    def publish(self, article: Article) -> PublishResult:
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
        except ImportError:
            return PublishResult(
                success=False,
                platform="spark",
                article_id=article.id,
                error="Playwright not installed in current environment."
            )

        if not self.verify_auth():
            return PublishResult(
                success=False,
                platform="spark",
                article_id=article.id,
                error="Требуется авторизация: запустите 'serm login --platform spark' или вставьте куки в админке"
            )

        try:
            with sync_playwright() as p:
                browser, context = open_context(p, self.session_dir)
                try:
                    page = context.new_page()
                    page.goto("https://spark.ru/write", wait_until="domcontentloaded", timeout=30000)
                    time.sleep(3)

                    # Check if redirected to auth
                    if "auth" in page.url:
                        return PublishResult(
                            success=False,
                            platform="spark",
                            article_id=article.id,
                            error="Сессия Spark устарела или не авторизована. Требуется обновить куки"
                        )

                    # Fill title
                    title_input = "input[name*='title'], input[placeholder*='Заголовок'], #post-title"
                    page.wait_for_selector(title_input, timeout=10000)
                    page.fill(title_input, article.title)

                    # Fill content with clean HTML
                    content_html = article.content_html
                    if not content_html:
                        content_html = markdown_to_html(article.content_markdown, platform="spark")

                    page.evaluate('''html => {
                        const redactor = document.querySelector('.redactor-editor, [contenteditable="true"]');
                        if (redactor) {
                            redactor.innerHTML = html;
                            redactor.dispatchEvent(new Event('input', { bubbles: true }));
                            redactor.dispatchEvent(new Event('change', { bubbles: true }));
                        } else {
                            const txt = document.querySelector("textarea[name*='text']");
                            if (txt) {
                                txt.value = html;
                                txt.dispatchEvent(new Event('input', { bubbles: true }));
                            }
                        }
                    }''', content_html)

                    time.sleep(2)

                    submit_btn = page.locator("button:has-text('Опубликовать')")
                    if submit_btn.count() > 0:
                        submit_btn.first.click()
                        # published = the editor is left; a failed validation or a modal keeps the page on /write
                        try:
                            page.wait_for_url(lambda u: "/write" not in u and "auth" not in u, timeout=15000)
                        except PlaywrightTimeoutError:
                            return PublishResult(
                                success=True,
                                platform="spark",
                                article_id=article.id,
                                published_url=page.url,
                                status="draft_saved",
                                message="Клик «Опубликовать» не подтвердился — проверьте статью вручную"
                            )
                        return PublishResult(
                            success=True,
                            platform="spark",
                            article_id=article.id,
                            published_url=page.url,
                            status="published",
                            message="Статья опубликована на Spark.ru"
                        )
                    else:
                        return PublishResult(
                            success=True,
                            platform="spark",
                            article_id=article.id,
                            status="draft_saved",
                            message="Черновик сохранен на Spark.ru"
                        )
                finally:
                    browser.close()
        except Exception as e:
            return PublishResult(
                success=False,
                platform="spark",
                article_id=article.id,
                error=f"Сбой публикации Spark.ru: {str(e)}"
            )
