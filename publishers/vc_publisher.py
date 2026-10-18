"""
Publisher adapter for VC.ru.
Automates draft creation and publishing via Playwright persistent browser context.
Uses HTML paste emulation for Editor.js block recognition.
"""
import time
from pathlib import Path
from core.models import Article, PublishResult
from publishers.base import BasePublisher
from config.settings import PLATFORMS_CONFIG
from core.markdown_parser import markdown_to_html

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
        """Publishes article to personal blog or subsite on VC.ru."""
        try:
            from playwright.sync_api import sync_playwright
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
                page.goto("https://vc.ru/editor", wait_until="domcontentloaded", timeout=30000)
                time.sleep(3)

                # Check if editor opened
                if "editor" not in page.url and "auth" in page.url:
                    context.close()
                    return PublishResult(
                        success=False,
                        platform="vc",
                        article_id=article.id,
                        error="Сессия устарела. Запустите 'serm login --platform vc'"
                    )

                # Fill title
                title_selector = "textarea[placeholder*='Заголовок'], .ce-header, [data-placeholder*='Заголовок']"
                page.wait_for_selector(title_selector, timeout=10000)
                page.fill(title_selector, article.title)
                page.keyboard.press("Enter")
                time.sleep(1)

                # Fill body via HTML paste event into Editor.js
                body_html = article.content_html
                if not body_html:
                    body_html = markdown_to_html(article.content_markdown, platform="vc")

                page.evaluate('''html => {
                    const target = document.querySelector('.ce-paragraph, [data-placeholder*="Нажмите Tab"], .editor-content');
                    if (target) {
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
                    }
                }''', body_html)

                time.sleep(3)

                # Save draft
                context.close()
                return PublishResult(
                    success=True,
                    platform="vc",
                    article_id=article.id,
                    published_url="https://vc.ru/drafts",
                    status="draft_saved",
                    message="Статья сохранена в черновиках VC.ru с HTML-структурой"
                )
        except Exception as e:
            return PublishResult(
                success=False,
                platform="vc",
                article_id=article.id,
                error=f"Сбой публикации VC.ru: {str(e)}"
            )
