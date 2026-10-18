"""
Content generation facade: articles, Telegram posts pack, lead magnet.

Branch is chosen by `profile.generation.mode`: no profile / no `generation` section -> `template`
(the existing deterministic generators, no LLM and no network). Mode `llm` -> core/llm_generation.py;
without an API key or on a provider error it raises and alerts the admin — there is no silent
fallback to `template` (a foreign profile must never get the built-in skeleton published).

`profile` is accepted as an object or a dict.
"""
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.models import Article, Campaign

logger = logging.getLogger("Content")

MODE_TEMPLATE = "template"
MODE_LLM = "llm"


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def generation_mode(profile: Optional[Any] = None, override: Optional[str] = None) -> str:
    """Profile generation.mode (no section → template); `override` is the CLI --mode."""
    mode = override or _get(_get(profile, "generation"), "mode") or MODE_TEMPLATE
    if mode not in (MODE_TEMPLATE, MODE_LLM):
        raise ValueError(f"Unknown generation mode: {mode}")
    return mode


def _alert(title: str, error: Exception) -> None:
    try:
        from core.alerts import notify_admin_sync
        notify_admin_sync(title, str(error), context="generation.mode=llm")
    except Exception as e:  # the alert must not mask the generation error
        logger.error(f"Cannot alert admin: {e}")


def _llm_client(client: Optional[Any], refresh_cache: bool):
    from core.llm_client import LLMClient, LLMNotConfigured
    client = client or LLMClient(refresh_cache=refresh_cache)
    if not client.is_configured():
        error = LLMNotConfigured("generation.mode=llm needs LLM_API_KEY (or POLZA_API_KEY) in .env")
        _alert("LLM generation is not configured", error)
        raise error
    return client


def _print_dry_run(items: List[Dict[str, Any]], max_tokens_estimate: Optional[Any]) -> None:
    # --dry-run: prompts and the cost upper bound are printed, the API is not called
    for item in items:
        print(f"\n===== {item['artifact']} {item.get('platform') or ''}".rstrip())
        for message in item["messages"]:
            print(f"--- {message['role']} ---\n{message['content']}")
    total = f"{max_tokens_estimate}" if max_tokens_estimate is not None else "unknown (LLM_PRICE_OUTPUT_PER_MTOK not set)"
    print(f"\nLLM calls: {len(items)}, cost upper bound by output tokens: {total}")


def _dry_run(profile: Any, items: List[Dict[str, Any]], client: Optional[Any]) -> None:
    from core.llm_client import LLMClient
    from core.llm_generation import as_profile
    client = client or LLMClient()
    one = client.estimate_cost(as_profile(profile).generation.max_tokens)
    _print_dry_run(items, one * len(items) if one is not None else None)


def _run_llm(title: str, call):
    from core.llm_client import LLMError
    from core.llm_generation import GenerationError
    try:
        return call()
    except (LLMError, GenerationError) as e:
        _alert(title, e)
        raise


def articles(campaign: Campaign, profile: Optional[Any] = None, news: Optional[Dict[str, Any]] = None,
             *, mode: Optional[str] = None, regenerate: bool = False, refresh_cache: bool = False,
             dry_run: bool = False, client: Optional[Any] = None) -> List[Article]:
    """
    One article per platform. Template: returned, not saved (as before). Llm: only platforms enabled
    in the profile, each saved to the db when ready; existing (campaign, platform) skipped unless
    `regenerate`; `news` is the news hook of the prompt; `refresh_cache` bypasses data/llm_cache;
    `dry_run` prints prompts and the cost bound and returns [] without calling the API.
    `mode` overrides profile.generation.mode.
    """
    if generation_mode(profile, mode) == MODE_LLM:
        from core import llm_generation
        if dry_run:
            from publishers.registry import enabled
            items = [{"artifact": "article", "platform": key,
                      "messages": llm_generation.article_messages(profile, campaign, key, news)}
                     for key in enabled(profile)]
            _dry_run(profile, items, client)
            return []
        llm = _llm_client(client, refresh_cache)
        return _run_llm(f"LLM articles failed ({campaign.id})", lambda: llm_generation.generate_articles(
            campaign, profile, news, regenerate=regenerate, client=llm))
    from core.generator import ContentGenerator
    return ContentGenerator(profile=profile).generate_campaign_articles(
        campaign_id=campaign.id,
        brand_name=campaign.brand_name,
        target_query=campaign.target_query,
        seed_topic=campaign.seed_topic,
    )


def tg_posts(campaign: Optional[Campaign], articles: Optional[List[Article]], profile: Optional[Any] = None,
             *, output_dir: Optional[Path] = None, mode: Optional[str] = None, refresh_cache: bool = False,
             dry_run: bool = False, client: Optional[Any] = None) -> Path:
    """Writes the posts pack; default location data/exports/campaign_<id>/. `dry_run` (llm): prompt only."""
    if output_dir is None:
        from config.settings import EXPORTS_DIR
        campaign_id = campaign.id if campaign else _get(profile, "campaign_id")
        output_dir = EXPORTS_DIR / f"campaign_{campaign_id}"
    if generation_mode(profile, mode) == MODE_LLM:
        from core import llm_generation
        if dry_run:
            _dry_run(profile, [{"artifact": "tg_posts", "messages": llm_generation.posts_messages(
                profile, campaign, articles)}], client)
            return Path(output_dir) / "telegram_content_pack.md"
        llm = _llm_client(client, refresh_cache)
        return _run_llm("LLM Telegram posts failed", lambda: llm_generation.generate_posts_pack(
            campaign, articles, profile, output_dir=output_dir, client=llm))
    from core.telegram_posts import TelegramPostsGenerator
    return TelegramPostsGenerator(output_dir, profile=profile).generate_posts_pack(campaign, articles)


async def lead_magnet(slug: str, profile: Optional[Any] = None, *, output_dir: Path, mode: Optional[str] = None,
                      refresh_cache: bool = False, dry_run: bool = False,
                      client: Optional[Any] = None) -> Dict[str, Any]:
    """Markdown + PDF for topic/slug `slug` in `output_dir`. Without a profile — generator defaults.
    `dry_run` (llm): prints the prompt, returns {} and writes nothing."""
    if generation_mode(profile, mode) == MODE_LLM:
        from core import llm_generation
        from core.llm_client import LLMError
        if dry_run:
            _dry_run(profile, [{"artifact": "lead_magnet", "platform": slug,
                                "messages": llm_generation.lead_magnet_messages(profile, slug)}], client)
            return {}
        llm = _llm_client(client, refresh_cache)
        try:
            return await llm_generation.generate_lead_magnet(slug, profile, output_dir=output_dir, client=llm)
        except (LLMError, llm_generation.GenerationError) as e:
            _alert(f"LLM lead magnet failed ({slug})", e)
            raise
    from core.lead_magnet import DynamicLeadMagnetGenerator
    # Presets and templates over the same profile as brand/contact (None → the installation profile)
    if isinstance(profile, dict):
        from core.llm_generation import as_profile
        profile = as_profile(profile)
    generator = DynamicLeadMagnetGenerator(output_dir, profile=profile)
    kwargs = {}
    brand_name = _get(profile, "display_name")
    contact = _get(_get(profile, "telegram"), "contact")
    if brand_name:
        kwargs["brand_name"] = brand_name
    if contact:
        kwargs["channel"] = contact
    return await generator.generate_for_topic(slug, **kwargs)
