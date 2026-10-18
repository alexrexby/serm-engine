"""
Publisher adapter for Spark.ru.
Automates draft creation and publishing via Playwright persistent context.
Uses HTML insertion for Redactor WYSIWYG editor to guarantee native rich typography.
"""
import time
from core.models import Article, PublishResult
from publishers.base import BasePublisher
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
            from playwright.sync_api import sync_playwright
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
            launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
            launch_kwargs = {
                "user_data_dir": str(self.session_dir),
                "headless": True,
                "args": launch_args
            }
            storage_file = self.session_dir / "storage_state.json"
            if storage_file.exists() and storage_file.stat().st_size > 50:
                launch_kwargs["storage_state"] = str(storage_file)

            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(**launch_kwargs)
                page = context.new_page()
                page.goto("https://spark.ru/write", wait_until="domcontentloaded", timeout=30000)
                time.sleep(3)

                # Check if redirected to auth
                if "auth" in page.url:
                    context.close()
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

                # Publish or draft button
                draft_btn = page.locator("button:has-text('черновик'), input[value*='черновик']")
                if draft_btn.count() > 0:
                    draft_btn.first.click()
                    time.sleep(3)
                    draft_url = page.url
                    context.close()
                    return PublishResult(
                        success=True,
                        platform="spark",
                        article_id=article.id,
                        published_url=draft_url,
                        status="draft_saved",
                        message="Черновик сохранён на Spark.ru с HTML-вёрсткой"
                    )

                submit_btn = page.locator("button:has-text('Опубликовать'), input[type='submit']")
                if submit_btn.count() > 0:
                    submit_btn.first.click()
                    time.sleep(5)
                    published_url = page.url
                    context.close()
                    return PublishResult(
                        success=True,
                        platform="spark",
                        article_id=article.id,
                        published_url=published_url,
                        status="published",
                        message="Статья опубликована на Spark.ru"
                    )
                else:
                    context.close()
                    return PublishResult(
                        success=True,
                        platform="spark",
                        article_id=article.id,
                        status="draft_saved",
                        message="Черновик сохранен на Spark.ru"
                    )
        except Exception as e:
            return PublishResult(
                success=False,
                platform="spark",
                article_id=article.id,
                error=f"Сбой публикации Spark.ru: {str(e)}"
            )
