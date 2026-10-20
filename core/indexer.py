"""
Fast Indexing Engine for SERM content.
Triggers immediate crawler pickup by Yandex & Google through high-frequency
crawled channels (Public Telegram channels, VKontakte wall posts, IndexNow).
"""
import json
import urllib.request
import urllib.parse
from typing import Dict, Any, Optional
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_INDEX_CHANNEL_ID, VK_API_ACCESS_TOKEN, VK_OWNER_ID
from core.models import IndexingResult


def indexable_url(result) -> Optional[str]:
    """URL of a publish result to ping, or None: a draft's edit URL must never reach the indexer."""
    return result.published_url if result.status == "published" and result.published_url else None


class FastIndexer:
    def __init__(
        self,
        tg_token: str = TELEGRAM_BOT_TOKEN,
        tg_channel: str = TELEGRAM_INDEX_CHANNEL_ID,
        vk_token: str = VK_API_ACCESS_TOKEN,
        vk_owner_id: str = VK_OWNER_ID
    ):
        self.tg_token = tg_token
        self.tg_channel = tg_channel
        self.vk_token = vk_token
        self.vk_owner_id = vk_owner_id

    def ping_all(self, article_id: str, title: str, published_url: str) -> Dict[str, IndexingResult]:
        """Runs fast-indexing pings across all configured channels."""
        results = {}
        results["telegram"] = self.ping_telegram(article_id, title, published_url)
        results["vk"] = self.ping_vk(article_id, title, published_url)
        results["indexnow"] = self.ping_indexnow(article_id, published_url)
        return results

    def ping_telegram(self, article_id: str, title: str, published_url: str) -> IndexingResult:
        """
        Sends link to a public Telegram channel.
        Yandex Fast-Robot crawls public Telegram web previews (t.me/s/) in near real-time.
        """
        if not self.tg_token or not self.tg_channel:
            return IndexingResult(
                article_id=article_id,
                channel="telegram",
                success=False,
                message="SERM_TG_BOT_TOKEN or SERM_TG_CHANNEL_ID not configured (skipped)"
            )

        text = f"🔥 <b>{title}</b>\n\nЧитать полный материал: {published_url}\n\n#SERM #обзор #статьи"
        endpoint = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
        payload = {
            "chat_id": self.tg_channel,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }

        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                if res_data.get("ok"):
                    return IndexingResult(
                        article_id=article_id,
                        channel="telegram",
                        success=True,
                        message=f"Successfully broadcasted to Telegram ({self.tg_channel})"
                    )
                else:
                    return IndexingResult(
                        article_id=article_id,
                        channel="telegram",
                        success=False,
                        message=f"Telegram API error: {res_data.get('description')}"
                    )
        except Exception as e:
            return IndexingResult(
                article_id=article_id,
                channel="telegram",
                success=False,
                message=f"Telegram request failed: {str(e)}"
            )

    def ping_vk(self, article_id: str, title: str, published_url: str) -> IndexingResult:
        """
        Posts article link to VK wall.
        VK posts are immediately crawled by Yandex search bots.
        """
        if not self.vk_token or not self.vk_owner_id:
            return IndexingResult(
                article_id=article_id,
                channel="vk",
                success=False,
                message="SERM_VK_TOKEN or SERM_VK_OWNER_ID not configured (skipped)"
            )

        endpoint = "https://api.vk.com/method/wall.post"
        message = f"{title}\n\nПодробнее: {published_url}"
        params = {
            "access_token": self.vk_token,
            "v": "5.199",
            "owner_id": self.vk_owner_id,
            "message": message,
            "attachments": published_url
        }

        try:
            data = urllib.parse.urlencode(params).encode("utf-8")
            req = urllib.request.Request(endpoint, data=data)
            with urllib.request.urlopen(req, timeout=10) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                if "response" in res_data:
                    return IndexingResult(
                        article_id=article_id,
                        channel="vk",
                        success=True,
                        message=f"VK post created (post_id: {res_data['response'].get('post_id')})"
                    )
                else:
                    return IndexingResult(
                        article_id=article_id,
                        channel="vk",
                        success=False,
                        message=f"VK API error: {res_data.get('error', {}).get('error_msg')}"
                    )
        except Exception as e:
            return IndexingResult(
                article_id=article_id,
                channel="vk",
                success=False,
                message=f"VK request failed: {str(e)}"
            )

    def ping_indexnow(self, article_id: str, published_url: str) -> IndexingResult:
        """
        IndexNow API ping (Yandex & Bing).
        Notifies search engines about a newly discovered URL.
        """
        try:
            parsed = urllib.parse.urlparse(published_url)
            host = parsed.netloc
            endpoint = "https://yandex.com/indexnow"
            payload = {
                "host": host,
                "urlList": [published_url]
            }
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"}
            )
            with urllib.request.urlopen(req, timeout=8) as response:
                code = response.getcode()
                return IndexingResult(
                    article_id=article_id,
                    channel="indexnow",
                    success=(code in (200, 202)),
                    message=f"IndexNow Yandex ping response: {code}"
                )
        except Exception as e:
            return IndexingResult(
                article_id=article_id,
                channel="indexnow",
                success=False,
                message=f"IndexNow notice: {str(e)}"
            )
