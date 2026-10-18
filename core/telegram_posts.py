"""
Telegram Content Pack Generator.
Produces ready-to-publish Telegram channel posts for each campaign from templates:
- `telegram/posts.yaml.j2` — order, ids and titles of the posts (each with its `template`);
- `telegram/posts/NN-<id>.md.j2` — post text (markdown: **bold**, [anchor](url), bot deep links);
- `telegram/pack.md.j2` — the pack file layout.
Profile override templates (profiles/<name>/templates/telegram/) replace the neutral built-in ones.
The autopilot sends the same posts one by one (`load_posts`).
"""
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from core.models import Campaign, Article
from core.templating import render

POSTS_INDEX = "telegram/posts.yaml.j2"
PACK_TEMPLATE = "telegram/pack.md.j2"


def _resolve_profile(profile: Optional[Any]) -> Any:
    if profile is not None:
        return profile
    from core.profile import get_profile
    return get_profile()


def _render(name: str, profile: Any, **ctx: Any) -> str:
    return render(name, profile, v=profile.v, magnet=profile.lead_magnet(), **ctx)


def load_posts(profile: Optional[Any] = None) -> List[Dict[str, str]]:
    """Posts of the profile in publishing order: [{id, title, content}]; content is stripped markdown."""
    profile = _resolve_profile(profile)
    index = yaml.safe_load(_render(POSTS_INDEX, profile)) or {}
    posts = []
    for entry in index.get("posts") or []:
        posts.append({
            "id": entry["id"],
            "title": entry["title"],
            "content": _render(entry["template"], profile, post_id=entry["id"]).strip(),
        })
    return posts


class TelegramPostsGenerator:
    """Generates Telegram Channel Content Packs for campaigns."""

    def __init__(self, output_dir: Path, profile: Optional[Any] = None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.profile = profile

    def generate_posts_pack(self, campaign: Optional[Campaign] = None, articles: Optional[List[Article]] = None) -> Path:
        target_path = self.output_dir / "telegram_content_pack.md"
        profile = _resolve_profile(self.profile)
        brand = campaign.brand_name if campaign else profile.display_name
        text = _render(PACK_TEMPLATE, profile, brand=brand, posts=load_posts(profile),
                       campaign=campaign, articles=articles or [])
        target_path.write_text(text, encoding="utf-8")
        return target_path
