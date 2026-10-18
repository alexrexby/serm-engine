"""
Template-mode article generator: one article per platform, rendered over the brand profile.

All reader-facing words live in templates (templates/articles/**, partials/offer.*); a profile
overrides any of them through its own templates directory. Article layout per platform:
title, italic lead, numbered sections with bodies, self-check table, offer block.

Templates (context: profile, v, brand, target_query, seed_topic, platform, campaign_id):
- articles/platforms.yaml.j2 — {platform: {title, tov, sections[]}}, order = generation order
- articles/intro.md.j2, articles/tags.txt.j2 — lead and tags (one line)
- articles/body/<platform>.md.j2 — body of section `step` (1-based), heading in `section`
- articles/audit_table.md.j2 / .html.j2 — self-check table
- partials/offer.md.j2 / .html.j2 — offer block, shared with the llm mode and the bot
Inline fragments (title, lead, tags, bodies, html table) are stripped; markdown blocks
(table, offer) are inserted as rendered.
"""
import uuid
from typing import Any, Dict, List, Optional

import yaml

from core.models import Article
from core.templating import render


class ContentGenerator:
    """Template-mode content generator. Profile: explicit, else the installation profile (get_profile)."""

    def __init__(self, api_key: str = "", profile: Optional[Any] = None):
        self.api_key = api_key
        self._profile = profile

    @property
    def profile(self) -> Any:
        if self._profile is None:
            from core.profile import get_profile
            self._profile = get_profile()
        return self._profile

    def _context(self, brand_name: str, target_query: str, seed_topic: str, **extra: Any) -> Dict[str, Any]:
        return {
            "v": self.profile.v,
            "brand": brand_name,
            "target_query": target_query,
            "seed_topic": seed_topic,
            **extra,
        }

    def _render(self, name: str, ctx: Dict[str, Any]) -> str:
        return render(name, self.profile, **ctx)

    def platforms(self, brand_name: str, target_query: str, seed_topic: str) -> Dict[str, Dict[str, Any]]:
        """Platform key -> {title, tov, sections} from articles/platforms.yaml.j2."""
        ctx = self._context(brand_name, target_query, seed_topic)
        data = yaml.safe_load(self._render("articles/platforms.yaml.j2", ctx))
        if not isinstance(data, dict):
            raise ValueError("articles/platforms.yaml.j2 must render to a mapping {platform: {...}}")
        return data

    def generate_campaign_articles(
        self,
        campaign_id: str,
        brand_name: str,
        target_query: str,
        seed_topic: str
    ) -> List[Article]:
        articles = []
        for platform_key, meta in self.platforms(brand_name, target_query, seed_topic).items():
            article = self._generate_business_article(
                campaign_id=campaign_id,
                platform=platform_key,
                brand_name=brand_name,
                target_query=target_query,
                seed_topic=seed_topic,
                meta=meta
            )
            articles.append(article)
        return articles

    def _generate_business_article(
        self,
        campaign_id: str,
        platform: str,
        brand_name: str,
        target_query: str,
        seed_topic: str,
        meta: Dict[str, Any]
    ) -> Article:
        ctx = self._context(brand_name, target_query, seed_topic, platform=platform, campaign_id=campaign_id)
        title = str(meta["title"]).strip()
        lead = self._render("articles/intro.md.j2", ctx).strip()

        sections_md = [f"# {title}\n", f"*{lead}*\n"]
        sections_html = [f"<h1>{title}</h1>", f"<p class='lead'><em>{lead}</em></p>"]

        for idx, sec_title in enumerate(meta["sections"], 1):
            sections_md.append(f"## {idx}. {sec_title}\n")
            sections_html.append(f"<h2>{idx}. {sec_title}</h2>")

            body = self._render(f"articles/body/{platform}.md.j2", {**ctx, "step": idx, "section": sec_title}).strip()
            sections_md.append(f"{body}\n")
            sections_html.append(f"<p>{body}</p>")

        # Self-check table, then the offer block
        sections_md.append(self._render("articles/audit_table.md.j2", ctx))
        sections_html.append(self._render("articles/audit_table.html.j2", ctx).strip())

        sections_md.append(self._render("partials/offer.md.j2", ctx))
        sections_html.append(self._render("partials/offer.html.j2", ctx))

        full_md = "\n".join(sections_md)
        full_html = "\n".join(sections_html)

        tags = self._render("articles/tags.txt.j2", ctx).strip()

        return Article(
            id=str(uuid.uuid4()),
            campaign_id=campaign_id,
            platform=platform,
            title=title,
            lead=lead,
            content_markdown=full_md,
            content_html=full_html,
            tags=tags,
            status="ready"
        )
