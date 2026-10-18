"""
News Radar: turns business news from RSS feeds into applied case breakdowns
and synthesizes a campaign (articles + lead magnet) from a selected item.

The code knows only the mechanics. Everything brand-specific comes from the profile:
- feeds and keyword filters — `profile.radar` (feeds, relevant_keywords, noise_keywords);
- ready-made cases and enrichment rules — template `radar/applied.yaml.j2`
  (rendered once over the profile, then yaml.safe_load; news text never enters it);
- campaign seed topic — template `radar/seed_topic.txt.j2`.
Content is generated through `core.content` with the news item as an infopovod.

RSS text is untrusted input: it is only matched against keywords here and passed on as data.
"""
import re
import ssl
import json
import asyncio
import logging
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Any, Optional, Set

import yaml

from config.settings import DATA_DIR

logger = logging.getLogger("NewsRadar")

# File name kept from the previous module: existing installations keep their cache.
CACHE_FILE = DATA_DIR / "horizon_feed_cache.json"

APPLIED_TEMPLATE = "radar/applied.yaml.j2"
SEED_TOPIC_TEMPLATE = "radar/seed_topic.txt.j2"
DEFAULT_SCORE = 5.0
MAX_ITEMS_PER_FEED = 2
MAX_TOP_ITEMS = 8

# Background lead magnet tasks: a reference keeps them from being garbage-collected mid-run
_LEAD_MAGNET_TASKS: Set[asyncio.Task] = set()


def _profile(profile: Optional[Any] = None):
    if profile is not None:
        return profile
    from core.profile import get_profile
    return get_profile()


def _render(name: str, profile: Any, **ctx: Any) -> str:
    from core.templating import render
    return render(name, profile, **ctx)


def applied_data(profile: Optional[Any] = None) -> Dict[str, Any]:
    """Rendered `radar/applied.yaml.j2`: evergreen_cases, rules, default."""
    profile = _profile(profile)
    data = yaml.safe_load(_render(APPLIED_TEMPLATE, profile)) or {}
    return {
        "evergreen_cases": list(data.get("evergreen_cases") or []),
        "rules": list(data.get("rules") or []),
        "default": dict(data.get("default") or {}),
    }


def evergreen_cases(profile: Optional[Any] = None) -> List[Dict[str, Any]]:
    return applied_data(profile)["evergreen_cases"]


def feeds(profile: Optional[Any] = None) -> List[Dict[str, Any]]:
    return [feed.model_dump() for feed in _profile(profile).radar.feeds]


class NewsRadar:
    """Fetches, parses and enriches business news into applied case breakdowns."""

    @staticmethod
    def is_noise(title: str, description: str, profile: Optional[Any] = None) -> bool:
        """Filters out non-actionable press releases and junk by the profile's keyword lists."""
        radar = _profile(profile).radar
        text = (title + " " + description).lower()
        if any(noise in text for noise in radar.noise_keywords):
            return True
        # With relevant keywords configured, an item must contain at least one of them
        if radar.relevant_keywords and not any(kw in text for kw in radar.relevant_keywords):
            return True
        return False

    @staticmethod
    def fetch_rss_feed(feed_url: str, max_items: int = 5, profile: Optional[Any] = None) -> List[Dict[str, Any]]:
        items = []
        try:
            req = urllib.request.Request(
                feed_url,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) NewsRadar/2.0"}
            )
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                xml_data = resp.read()
            items = NewsRadar.parse_rss(xml_data, max_items=max_items, profile=profile)
        except Exception as e:
            logger.warning(f"Error fetching RSS feed {feed_url}: {e}")
        return items

    @staticmethod
    def parse_rss(xml_data: bytes, max_items: int = 5, profile: Optional[Any] = None) -> List[Dict[str, Any]]:
        """RSS 2.0 <channel><item> → non-noise items {title, link, description, published_at}."""
        items = []
        root = ET.fromstring(xml_data)
        channel = root.find("channel")
        if channel is None:
            return items
        for entry in channel.findall("item"):
            title = (entry.findtext("title") or "").strip()
            link = (entry.findtext("link") or "").strip()
            desc = (entry.findtext("description") or "").strip()
            pub_date = (entry.findtext("pubDate") or "").strip()

            clean_desc = desc
            if "<" in desc and ">" in desc:
                clean_desc = re.sub(r"<[^>]+>", "", desc).strip()

            if title and not NewsRadar.is_noise(title, clean_desc, profile=profile):
                items.append({
                    "title": title,
                    "link": link,
                    "description": clean_desc[:250] + ("..." if len(clean_desc) > 250 else ""),
                    "published_at": pub_date
                })
                if len(items) >= max_items:
                    break
        return items

    @classmethod
    def enrich_item_to_vibecoding_case(cls, item: Dict[str, Any], profile: Optional[Any] = None) -> Dict[str, Any]:
        """Adds the fields of the first matching profile rule (or the default) to a news item."""
        data = applied_data(profile)
        t = (item.get("title", "") + " " + item.get("description", "")).lower()

        fields = data["default"]
        for rule in data["rules"]:
            if any(k in t for k in rule.get("match") or []):
                fields = rule.get("fields") or {}
                break
        item.update(fields)
        if not item.get("applied_title"):
            item["applied_title"] = item.get("title", "")
        return item

    @classmethod
    def collect_all_sources(cls, profile: Optional[Any] = None) -> List[Dict[str, Any]]:
        """Collects, filters, enriches and combines external RSS with the profile's evergreen cases."""
        profile = _profile(profile)
        enriched_news = []

        # 1. Start with evergreen cases to guarantee immediate value
        for eg in evergreen_cases(profile):
            enriched_news.append(dict(eg))

        # 2. Try fetching fresh relevant items from RSS
        for feed in profile.radar.feeds:
            raw_items = cls.fetch_rss_feed(feed.url, max_items=MAX_ITEMS_PER_FEED, profile=profile)
            for item in raw_items:
                item["source_name"] = feed.name
                item["category"] = feed.category
                enriched = cls.enrich_item_to_vibecoding_case(item, profile=profile)
                enriched_news.append(enriched)

        # Sort by business score
        enriched_news.sort(key=lambda x: x.get("business_score", DEFAULT_SCORE), reverse=True)

        # Deduplicate to ensure varied, distinct cases
        seen_titles = set()
        top_news = []
        for item in enriched_news:
            t_key = item.get("applied_title") or item.get("title")
            if t_key not in seen_titles:
                seen_titles.add(t_key)
                top_news.append(item)
            if len(top_news) >= MAX_TOP_ITEMS:
                break

        # Cache results
        CACHE_FILE.write_text(json.dumps(top_news, indent=2, ensure_ascii=False), encoding="utf-8")
        return top_news

    @classmethod
    def get_latest_news(cls, force_refresh: bool = False, profile: Optional[Any] = None) -> List[Dict[str, Any]]:
        if not force_refresh and CACHE_FILE.exists():
            try:
                data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
                if data and isinstance(data, list) and len(data) > 0 and "applied_title" in data[0]:
                    return data
            except Exception:
                pass
        return cls.collect_all_sources(profile=profile)

    @staticmethod
    def news_context(news_item: Dict[str, Any]) -> Dict[str, Any]:
        """Infopovod for generation (Contract 7: {title, summary, url, source}) — passed as data, not instructions."""
        return {
            "title": news_item.get("title", ""),
            "summary": news_item.get("description", ""),
            "url": news_item.get("link", ""),
            "source": news_item.get("source_name", ""),
        }

    @classmethod
    def synthesize_campaign_from_news(
        cls,
        news_item: Dict[str, Any],
        brand_name: Optional[str] = None,
        target_query: Optional[str] = None,
        profile: Optional[Any] = None
    ) -> Dict[str, Any]:
        """Creates a campaign in DB and generates content based on selected news item.

        Synchronous articles + the lead magnet as a task in the running loop. From a coroutine
        that must stay responsive (admin handler) call prepare_campaign_from_news in a worker
        thread and start_lead_magnet in the loop instead.
        """
        profile = _profile(profile)
        res = cls.prepare_campaign_from_news(news_item, brand_name, target_query, profile=profile)
        cls.start_lead_magnet(res, profile)
        return res

    @staticmethod
    def start_lead_magnet(res: Dict[str, Any], profile: Any) -> asyncio.Task:
        """Generates the campaign's lead magnet in the running loop; a failure is logged, not lost."""
        from core import content

        task = asyncio.create_task(
            content.lead_magnet(res["lead_magnet_preset"], profile, output_dir=Path(res["exports_dir"]))
        )
        _LEAD_MAGNET_TASKS.add(task)

        def done(t: asyncio.Task) -> None:
            _LEAD_MAGNET_TASKS.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("Lead magnet for campaign %s failed", res.get("campaign_id"), exc_info=t.exception())

        task.add_done_callback(done)
        return task

    @classmethod
    def prepare_campaign_from_news(
        cls,
        news_item: Dict[str, Any],
        brand_name: Optional[str] = None,
        target_query: Optional[str] = None,
        profile: Optional[Any] = None
    ) -> Dict[str, Any]:
        """Synchronous part of the synthesis: campaign in DB, articles, markdown exports, lead magnet preset.

        brand_name / target_query default to the profile (display_name, seo.target_query).
        The lead magnet (start_lead_magnet) is signed with the profile's display_name and contact.
        """
        import uuid
        from core import content
        from core.models import Campaign
        from core.db import Database
        from core.lead_magnet import DynamicLeadMagnetGenerator
        from config.settings import EXPORTS_DIR

        profile = _profile(profile)
        brand_name = brand_name or profile.display_name
        target_query = target_query or profile.seo.target_query

        db = Database()
        campaign_id = str(uuid.uuid4())[:8]
        case_title = news_item.get("applied_title") or news_item.get("title", "")
        seed_topic = _render(SEED_TOPIC_TEMPLATE, profile, case_title=case_title).strip()

        campaign = Campaign(
            id=campaign_id,
            brand_name=brand_name,
            target_query=target_query,
            seed_topic=seed_topic
        )
        db.add_campaign(campaign)

        # One article per platform
        articles = content.articles(campaign, profile, news=cls.news_context(news_item))
        for a in articles:
            db.add_article(a)

        # Export markdown articles
        camp_dir = EXPORTS_DIR / f"campaign_{campaign_id}"
        camp_dir.mkdir(parents=True, exist_ok=True)
        for a in articles:
            p = camp_dir / f"{a.platform}_{a.title[:30].replace(' ', '_')}.md"
            p.write_text(a.content_markdown, encoding="utf-8")

        # Detect and generate tailored lead magnet
        preset_key = news_item.get("lead_magnet_preset") or DynamicLeadMagnetGenerator(camp_dir).detect_preset_key(
            news_item.get("title", "") + " " + news_item.get("description", "")
        )

        return {
            "campaign_id": campaign_id,
            "seed_topic": seed_topic,
            "articles_count": len(articles),
            "lead_magnet_preset": preset_key,
            "exports_dir": str(camp_dir)
        }
