"""
LLM generation (generation.mode = llm): articles, Telegram posts pack, lead magnet.

The model writes only the body. Everything that must be exact — links, offer, deep links,
contacts — is added afterwards by templates (partials/offer.*, prompts/output/*), and the code
checks the answer instead of trusting the prompt:
1. body length (markdown stripped) within the platform min_chars/max_chars;
2. profile voice.forbidden_words / forbidden_topics (case-insensitive);
3. links and @handles: only the profile ones (telegram channel/contact/bot, deploy.server_url),
   the rest are cut out with a warning;
4. HTML is produced by core/markdown_parser from escaped text, never by the model.
A failed check (1, 2 or broken JSON) gets one repair request with the errors; if it fails again,
the article is saved as draft with error_message (posts pack and lead magnet raise instead:
they have no draft state, and the bot sends the lead magnet to subscribers as is).
Only an answer that passed the checks is cached, under the key of the first request: a rerun
gets the good answer for 0, a failed one is asked again.

Prompt injection: news/RSS text goes into the user message inside a «<<< … >>>» data block,
fence markers are stripped from it, and the system prompt says the block is data, not instructions.
"""
import asyncio
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.llm_client import LLMClient
from core.markdown_parser import markdown_to_html, to_plain_text
from core.models import Article, Campaign
from core.profile import Profile
from core.templating import render

logger = logging.getLogger("LLMGeneration")

ARTIFACT_ARTICLE = "article"
ARTIFACT_TG_POSTS = "tg_posts"
ARTIFACT_LEAD_MAGNET = "lead_magnet"

TG_POST_MAX_CHARS = 3500  # Telegram message limit is 4096; the rest is left for the footer
NEWS_LIMITS = {"title": 300, "summary": 2000, "source": 120}
TARGET_LENGTH_FACTOR = 1.3  # aim above min_chars so a slightly short answer still passes

_FENCE_RE = re.compile(r"<{3,}|>{3,}")
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)\s]*)[^)]*\)")
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]\"'«»]+|(?<![\w/.@])(?:t|telegram)\.me/[A-Za-z0-9_/?=&%-]+",
                     re.IGNORECASE)
_HANDLE_RE = re.compile(r"(?<![\w@/.])@([A-Za-z][A-Za-z0-9_]{3,31})\b")
_TRAILING_PUNCT = ".,;:!?"


class GenerationError(RuntimeError):
    """The answer did not pass the checks after the repair request."""


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ArticleDraft(_Lenient):
    title: str = Field(min_length=1)
    lead: str = ""
    body_markdown: str = Field(min_length=1)
    tags: List[str] = Field(default_factory=list)


class PostDraft(_Lenient):
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)


class PostsDraft(_Lenient):
    posts: List[PostDraft] = Field(min_length=1)


class MagnetSection(_Lenient):
    heading: str = Field(min_length=1)
    items: List[str] = Field(min_length=1)


class MagnetDraft(_Lenient):
    title: str = Field(min_length=1)
    sections: List[MagnetSection] = Field(min_length=1)


# --- profile & context ------------------------------------------------------

def as_profile(profile: Any) -> Profile:
    if isinstance(profile, Profile):
        return profile
    if isinstance(profile, dict):
        return Profile.model_validate(profile)
    raise TypeError("llm generation needs a profile (core.profile.Profile or dict)")


def platform_context(key: str) -> Dict[str, Any]:
    from publishers.registry import PLATFORMS
    spec = PLATFORMS[key]
    min_chars = int(spec.limits.get("min_chars") or 0)
    max_chars = spec.limits.get("max_chars")
    target = int(min_chars * TARGET_LENGTH_FACTOR)
    if max_chars:
        target = min(target, int(max_chars))
    return {
        "key": key,
        "name": spec.name,
        "url": spec.url,
        "type": spec.type,
        "min_chars": min_chars,
        "max_chars": int(max_chars) if max_chars else None,
        "target_chars": target,
        "allow_markdown": bool(spec.limits.get("allow_markdown", True)),
    }


def safe_news(news: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """News item → plain fields for the data block: fence markers removed, lengths capped."""
    if not news:
        return None
    clean = {}
    for name, limit in NEWS_LIMITS.items():
        value = str(news.get(name) or "")
        value = _FENCE_RE.sub(" ", value)
        value = re.sub(r"\s+", " ", value).strip()
        clean[name] = value[:limit]
    return clean if clean["title"] or clean["summary"] else None


def _seo(profile: Profile, campaign: Optional[Campaign]) -> Dict[str, str]:
    return {
        "target_query": (campaign.target_query if campaign else "") or profile.seo.target_query,
        "seed_topic": (campaign.seed_topic if campaign else "") or profile.seo.seed_topic,
    }


def _context(profile: Profile, campaign: Optional[Campaign], news: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "v": profile.v,
        "brand": (campaign.brand_name if campaign else "") or profile.display_name,
        "seo": _seo(profile, campaign),
        "news": safe_news(news),
    }


def system_prompt(profile: Any) -> str:
    profile = as_profile(profile) if isinstance(profile, dict) else profile
    return render("prompts/system.txt.j2", profile, v=profile.v)


def _messages(profile: Profile, user: str) -> List[Dict[str, str]]:
    return [{"role": "system", "content": system_prompt(profile)}, {"role": "user", "content": user}]


def article_messages(profile: Any, campaign: Optional[Campaign], platform: str,
                     news: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    profile = as_profile(profile)
    user = render(f"prompts/article/{platform}.md.j2", profile,
                  platform=platform_context(platform), **_context(profile, campaign, news))
    return _messages(profile, user)


def posts_messages(profile: Any, campaign: Optional[Campaign], articles: Optional[List[Article]],
                   news: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    profile = as_profile(profile)
    items = [{"title": a.title, "platform": a.platform} for a in (articles or [])]
    user = render("prompts/tg_post.md.j2", profile, articles=items, max_chars=TG_POST_MAX_CHARS,
                  **_context(profile, campaign, news))
    return _messages(profile, user)


def lead_magnet_messages(profile: Any, slug: str) -> List[Dict[str, str]]:
    profile = as_profile(profile)
    magnet = _magnet(profile, slug)
    user = render("prompts/lead_magnet.md.j2", profile, magnet=magnet, **_context(profile, None, None))
    return _messages(profile, user)


def _magnet(profile: Profile, slug: str):
    magnet = profile.lead_magnet(slug) or profile.lead_magnet()
    if magnet is None:
        raise GenerationError(f"profile has no lead magnet '{slug}' and no default_lead_magnet")
    return magnet


# --- answer parsing & checks -------------------------------------------------

def parse_answer(text: str, model_cls: Type[BaseModel]) -> BaseModel:
    """JSON answer (optionally in a ``` fence) → validated model; ValueError with a short reason."""
    raw = (text or "").strip()
    fenced = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    if not raw.startswith("{"):
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object in the answer")
        raw = raw[start:end + 1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e.msg} at {e.pos}") from None
    try:
        return model_cls.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(part) for part in first["loc"]) or "<root>"
        raise ValueError(f"{loc}: {first['msg']}") from None


class Allowlist:
    """Links and @handles the generated text may keep: only those of the profile."""

    def __init__(self, profile: Profile):
        tg = profile.telegram
        handles = [tg.contact, tg.bot_username or ""]
        if tg.channel.startswith("@"):
            handles.append(tg.channel)
        self.handles = {h.lstrip("@").lower() for h in handles if h}
        server = (profile.deploy.server_url or "").rstrip("/").lower()
        self.prefixes = [server] if server else []

    def handle_ok(self, name: str) -> bool:
        return name.lower() in self.handles

    def url_ok(self, url: str) -> bool:
        low = url.lower()
        if any(low == p or low.startswith(p + "/") for p in self.prefixes):
            return True
        tme = re.match(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/([A-Za-z0-9_]+)/?$", low)
        return bool(tme and self.handle_ok(tme.group(1)))


def strip_foreign(text: str, allow: Allowlist) -> Tuple[str, List[str]]:
    """Removes links and @handles that are not in the allowlist; markdown links keep their text."""
    removed: List[str] = []

    def md_link(m):
        if allow.url_ok(m.group(2)):
            return m.group(0)
        removed.append(m.group(2))
        return m.group(1)

    def url(m):
        value = m.group(0)
        stripped = value.rstrip(_TRAILING_PUNCT)
        tail = value[len(stripped):]
        if allow.url_ok(stripped):
            return value
        removed.append(stripped)
        return tail

    def handle(m):
        if allow.handle_ok(m.group(1)):
            return m.group(0)
        removed.append(m.group(0))
        return ""

    out = _MD_LINK_RE.sub(md_link, text)
    out = _URL_RE.sub(url, out)
    out = _HANDLE_RE.sub(handle, out)
    if removed:
        out = re.sub(r"[ \t]{2,}", " ", out)
        out = re.sub(r"[ \t]+([.,;:!?])", r"\1", out)
    return out, removed


def find_forbidden(text: str, profile: Profile) -> List[str]:
    terms = list(profile.voice.forbidden_words) + list(profile.voice.forbidden_topics)
    found = []
    for term in terms:
        term = term.strip()
        if term and re.search(r"(?<!\w)" + re.escape(term), text, re.IGNORECASE):
            found.append(term)
    return found


def length_issue(body_markdown: str, platform: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    actual = len(to_plain_text(body_markdown))
    if platform["min_chars"] and actual < platform["min_chars"]:
        return {"code": "too_short", "actual": actual, "min": platform["min_chars"]}
    if platform["max_chars"] and actual > platform["max_chars"]:
        return {"code": "too_long", "actual": actual, "max": platform["max_chars"]}
    return None


def describe_issues(issues: List[Dict[str, Any]]) -> str:
    parts = []
    for issue in issues:
        if issue["code"] == "too_short":
            parts.append(f"body too short: {issue['actual']} < {issue['min']} chars")
        elif issue["code"] == "too_long":
            parts.append(f"body too long: {issue['actual']} > {issue['max']} chars")
        elif issue["code"] == "forbidden":
            parts.append(f"forbidden words/topics: {', '.join(issue['words'])}")
        elif issue["code"] == "bad_json":
            parts.append(f"answer is not valid JSON: {issue['detail']}")
        else:
            parts.append(issue["code"])
    return "; ".join(parts)


def _ask_checked(
    client: LLMClient,
    profile: Profile,
    messages: List[Dict[str, str]],
    model_cls: Type[BaseModel],
    check: Callable[[BaseModel], Tuple[BaseModel, List[Dict[str, Any]]]],
    meta: Dict[str, Any],
) -> Tuple[Optional[BaseModel], List[Dict[str, Any]], str]:
    """Call → parse → sanitize/check; one repair request on failure. Returns (draft, issues, last raw text)."""
    gen = profile.generation
    draft, issues, text, first_key = None, [], "", None
    for attempt in (1, 2):
        result = client.complete(messages, model=gen.model, temperature=gen.temperature,
                                 max_tokens=gen.max_tokens, meta=meta, store=False)
        first_key = first_key or result.key
        text = result.text
        try:
            parsed = parse_answer(text, model_cls)
        except ValueError as e:
            draft, issues = None, [{"code": "bad_json", "detail": str(e)}]
        else:
            draft, issues = check(parsed)
        if not issues:
            if not result.cached:
                client.store(first_key, result)
            return draft, [], text
        logger.warning(f"LLM {meta.get('artifact')} {meta.get('platform') or ''}: check failed "
                       f"(attempt {attempt}): {describe_issues(issues)}")
        if attempt == 1:
            repair = render("prompts/repair.md.j2", profile, issues=issues)
            messages = messages + [{"role": "assistant", "content": text}, {"role": "user", "content": repair}]
    return draft, issues, text


def _clean(value: str, allow: Allowlist, removed: List[str]) -> str:
    out, cut = strip_foreign(value, allow)
    removed.extend(cut)
    return out


def _warn_removed(removed: List[str], where: str) -> None:
    if removed:
        logger.warning(f"LLM {where}: removed {len(removed)} foreign link(s)/handle(s): "
                       f"{', '.join(r[:80] for r in removed[:10])}")


# --- articles -----------------------------------------------------------------

def _escape_html_source(text: str) -> str:
    # markdown_parser does not escape: model text must not inject tags; '>' stays for blockquotes
    return text.replace("&", "&amp;").replace("<", "&lt;")


def _article_markdown(title: str, lead: str, body: str, offer_md: str) -> str:
    head = f"# {title}\n\n"
    if lead:
        head += f"*{lead}*\n\n"
    return f"{head}{body.strip()}\n\n{offer_md.strip()}\n"


def build_article(profile: Profile, campaign: Campaign, platform: str, draft: Optional[ArticleDraft],
                  raw_text: str, issues: List[Dict[str, Any]]) -> Article:
    spec = platform_context(platform)
    offer_md = render("partials/offer.md.j2", profile, v=profile.v)
    if draft is not None:
        title, lead, body = draft.title.strip(), draft.lead.strip(), draft.body_markdown
        tags = []
        for tag in list(draft.tags) + list(profile.aliases):
            tag = tag.strip()
            if tag and tag.lower() not in {t.lower() for t in tags}:
                tags.append(tag)
    else:
        # nothing parseable: keep the raw answer for the reviewer, the draft is never published as is
        title, lead, body, tags = f"{spec['name']}: {_seo(profile, campaign)['seed_topic']}", "", raw_text, []

    content_markdown = _article_markdown(title, lead, body, offer_md)
    html_source = _article_markdown(_escape_html_source(title), _escape_html_source(lead),
                                    _escape_html_source(body), offer_md)
    if issues:
        status = "draft"
    else:
        status = "draft" if profile.generation.review_required else "ready"
    return Article(
        id=str(uuid.uuid4()),
        campaign_id=campaign.id,
        platform=platform,
        title=title,
        lead=lead,
        content_markdown=content_markdown,
        content_html=markdown_to_html(html_source, platform=platform),
        tags=", ".join(tags),
        status=status,
        error_message=("LLM checks failed: " + describe_issues(issues)) if issues else None,
    )


def generate_article(client: LLMClient, profile: Profile, campaign: Campaign, platform: str,
                     news: Optional[Dict[str, Any]] = None) -> Article:
    spec = platform_context(platform)
    allow = Allowlist(profile)

    def check(draft: ArticleDraft):
        removed: List[str] = []
        draft = ArticleDraft(
            title=_clean(draft.title, allow, removed) or draft.title,
            lead=_clean(draft.lead, allow, removed),
            body_markdown=_clean(draft.body_markdown, allow, removed) or draft.body_markdown,
            tags=[t for t in draft.tags if not strip_foreign(t, allow)[1]],
        )
        _warn_removed(removed, f"article {platform}")
        issues = []
        problem = length_issue(draft.body_markdown, spec)
        if problem:
            issues.append(problem)
        words = find_forbidden("\n".join([draft.title, draft.lead, draft.body_markdown, *draft.tags]), profile)
        if words:
            issues.append({"code": "forbidden", "words": words})
        return draft, issues

    meta = {"campaign_id": campaign.id, "artifact": ARTIFACT_ARTICLE, "platform": platform}
    draft, issues, text = _ask_checked(client, profile, article_messages(profile, campaign, platform, news),
                                       ArticleDraft, check, meta)
    return build_article(profile, campaign, platform, draft, text, issues)


def generate_articles(campaign: Campaign, profile: Any, news: Optional[Dict[str, Any]] = None, *,
                      regenerate: bool = False, client: Optional[LLMClient] = None, db: Any = None) -> List[Article]:
    """
    One article per platform enabled in the profile, each saved to the db as soon as it is ready
    (BudgetExceeded / provider errors stop the run; what is saved stays saved).
    A platform that already has an article in this campaign is skipped unless regenerate=True
    (then a new article is added, the old one is kept, and the cache is bypassed: a cached
    answer would repeat the old article word for word).
    """
    from publishers.registry import enabled
    profile = as_profile(profile)
    client = client or LLMClient()
    if db is None:
        from core.db import Database
        db = Database()

    existing = {a.platform for a in db.list_articles_by_campaign(campaign.id)}
    created = []
    # regenerate bypasses the cache only for this call: the caller's client may go on to posts / lead magnet
    refresh_before = client.refresh_cache
    if regenerate:
        client.refresh_cache = True
    try:
        for platform in enabled(profile):
            if platform in existing and not regenerate:
                logger.info(f"LLM article {campaign.id}/{platform}: exists, skipped (use regenerate)")
                continue
            article = generate_article(client, profile, campaign, platform, news)
            db.add_article(article)
            created.append(article)
    finally:
        client.refresh_cache = refresh_before
    return created


# --- Telegram posts pack --------------------------------------------------------

def generate_posts_pack(campaign: Optional[Campaign], articles: Optional[List[Article]], profile: Any, *,
                        output_dir: Path, client: Optional[LLMClient] = None,
                        news: Optional[Dict[str, Any]] = None) -> Path:
    """Writes telegram_content_pack.md (same file as the template mode) via telegram/pack.md.j2."""
    profile = as_profile(profile)
    client = client or LLMClient()
    allow = Allowlist(profile)

    def check(draft: PostsDraft):
        removed: List[str] = []
        posts = [PostDraft(title=_clean(p.title, allow, removed) or p.title,
                           body=(_clean(p.body, allow, removed) or p.body).replace("```", "'''"))
                 for p in draft.posts]
        _warn_removed(removed, "tg posts")
        issues = []
        too_long = [len(p.body) for p in posts if len(p.body) > TG_POST_MAX_CHARS]
        if too_long:
            issues.append({"code": "too_long", "actual": max(too_long), "max": TG_POST_MAX_CHARS})
        words = find_forbidden("\n".join(f"{p.title}\n{p.body}" for p in posts), profile)
        if words:
            issues.append({"code": "forbidden", "words": words})
        return PostsDraft(posts=posts), issues

    campaign_id = campaign.id if campaign else profile.campaign_id
    meta = {"campaign_id": campaign_id, "artifact": ARTIFACT_TG_POSTS, "platform": "telegram"}
    draft, issues, _ = _ask_checked(client, profile, posts_messages(profile, campaign, articles, news),
                                    PostsDraft, check, meta)
    if issues:
        raise GenerationError(f"Telegram posts for {campaign_id}: {describe_issues(issues)}")

    footer = render("prompts/output/post_footer.md.j2", profile).strip()
    posts = [{"id": f"llm_{i}", "title": p.title.strip(), "content": f"{p.body.strip()}\n\n{footer}"}
             for i, p in enumerate(draft.posts, 1)]
    brand = (campaign.brand_name if campaign else "") or profile.display_name
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "telegram_content_pack.md"
    target.write_text(render("telegram/pack.md.j2", profile, brand=brand, posts=posts), encoding="utf-8")
    return target


# --- lead magnet -----------------------------------------------------------------

async def html_to_pdf(html: str, pdf_path: Path) -> None:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await browser.new_page()
        await page.set_content(html, wait_until="networkidle")
        await page.pdf(path=str(pdf_path), format="A4", print_background=True,
                       margin={"top": "8mm", "bottom": "8mm", "left": "8mm", "right": "8mm"})
        await browser.close()


def lead_magnet_draft(profile: Profile, slug: str, client: LLMClient) -> MagnetDraft:
    allow = Allowlist(profile)

    def check(draft: MagnetDraft):
        removed: List[str] = []
        sections = [MagnetSection(heading=_clean(s.heading, allow, removed) or s.heading,
                                  items=[_clean(i, allow, removed) or i for i in s.items])
                    for s in draft.sections]
        title = _clean(draft.title, allow, removed) or draft.title
        _warn_removed(removed, f"lead magnet {slug}")
        text = "\n".join([title] + [s.heading + "\n" + "\n".join(s.items) for s in sections])
        words = find_forbidden(text, profile)
        return MagnetDraft(title=title, sections=sections), ([{"code": "forbidden", "words": words}] if words else [])

    meta = {"campaign_id": profile.campaign_id, "artifact": ARTIFACT_LEAD_MAGNET, "platform": slug}
    draft, issues, _ = _ask_checked(client, profile, lead_magnet_messages(profile, slug), MagnetDraft, check, meta)
    if issues:
        raise GenerationError(f"lead magnet {slug}: {describe_issues(issues)}")
    return draft


async def generate_lead_magnet(slug: str, profile: Any, *, output_dir: Path,
                               client: Optional[LLMClient] = None) -> Dict[str, Any]:
    """lead_magnet_<slug>.md/.pdf (+ the default lead_magnet_15_business_holes.* copy, as the template mode)."""
    profile = as_profile(profile)
    client = client or LLMClient()
    magnet = _magnet(profile, slug)
    draft = await asyncio.to_thread(lead_magnet_draft, profile, magnet.slug, client)

    ctx = {"magnet": magnet, "draft": draft.model_dump(), "v": profile.v}
    md_content = render("prompts/output/lead_magnet.md.j2", profile, **ctx)
    html_content = render("prompts/output/lead_magnet.html.j2", profile, **ctx)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    file_slug = re.sub(r"[^a-zA-Z0-9_]", "_", magnet.slug.lower())[:30]
    md_path = output_dir / f"lead_magnet_{file_slug}.md"
    pdf_path = output_dir / f"lead_magnet_{file_slug}.pdf"
    md_path.write_text(md_content, encoding="utf-8")
    await html_to_pdf(html_content, pdf_path)

    copies = {output_dir / "lead_magnet_15_business_holes.pdf", output_dir / magnet.file_prefix}
    for copy in copies:
        if copy != pdf_path:
            copy.write_bytes(pdf_path.read_bytes())
    (output_dir / "lead_magnet_15_business_holes.md").write_text(md_content, encoding="utf-8")

    return {
        "markdown": md_path,
        "pdf": pdf_path,
        "preset_key": magnet.slug,
        "title": draft.title,
        "slug": file_slug,
        "size_bytes": pdf_path.stat().st_size,
    }
