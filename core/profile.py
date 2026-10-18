"""
Brand Profile: who speaks, to whom, and with which offer.
A profile is a YAML file in profiles/<name>.yaml validated by a strict schema
(unknown keys fail on load, so a typo is caught at startup, not in a published text).

Resolution order (resolve_profile_path):
1. SERM_PROFILE — a path to a YAML file or a profile name inside profiles/.
2. The single non-example profiles/*.yaml (a configured installation).
Otherwise ProfileError: example profiles are picked only explicitly (SERM_PROFILE=example-person),
never as a silent fallback — a server missing its profile must not speak as a fictional brand.
"""
import os
import re
import logging
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator, model_validator

logger = logging.getLogger("Profile")

# Computed locally: config.settings creates data/ on import, the profile must not.
BASE_DIR = Path(__file__).resolve().parent.parent
PROFILES_DIR = BASE_DIR / "profiles"
EXAMPLE_PREFIX = "example"

SCHEMA_VERSION = 1

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_HANDLE_RE = re.compile(r"^@[A-Za-z0-9_]{4,32}$")
_CHANNEL_ID_RE = re.compile(r"^-100\d+$")
_BOT_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
# Bot callback_data is tier_<id>, and Telegram caps callback_data at 64 bytes
_TIER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,57}$")


class ProfileError(Exception):
    """Profile cannot be found, parsed or validated."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VoiceSettings(_Strict):
    pronoun: Literal["auto", "я", "мы"] = "auto"
    tone: str = ""
    theses: List[str] = Field(default_factory=list)
    forbidden_words: List[str] = Field(default_factory=list)
    forbidden_topics: List[str] = Field(default_factory=list)
    system_prompt_extra: str = ""


class SeoSettings(_Strict):
    target_query: str
    seed_topic: str = ""


class TelegramSettings(_Strict):
    channel: str
    channel_title: str = ""
    contact: str
    bot_username: Optional[str] = None

    @field_validator("channel")
    @classmethod
    def _check_channel(cls, value: str) -> str:
        if not (_HANDLE_RE.match(value) or _CHANNEL_ID_RE.match(value)):
            raise ValueError("expected @channel_name or -100… chat id")
        return value

    @field_validator("contact")
    @classmethod
    def _check_contact(cls, value: str) -> str:
        if not _HANDLE_RE.match(value):
            raise ValueError("expected @username")
        return value

    @field_validator("bot_username")
    @classmethod
    def _check_bot_username(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.lstrip("@")
        if not _BOT_USERNAME_RE.match(value):
            raise ValueError("expected bot username without @, e.g. example_bot")
        return value


class OfferTier(_Strict):
    id: str
    title: str
    text: str = ""
    cta: str = ""
    url: Optional[str] = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _TIER_ID_RE.match(value):
            raise ValueError("tier id must be lowercase latin, digits, _ or -, at most 58 characters")
        return value


class LeadMagnet(_Strict):
    slug: str  # written to bot_events.slug — never rename on a live installation
    match: List[str] = Field(default_factory=list)
    title: str
    download_filename: str
    file_prefix: str
    desc: str = ""
    topic_name: str = ""
    template: str = "lead_magnet/checklist"

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, value: str) -> str:
        if not _SLUG_RE.match(value):
            raise ValueError("slug must be lowercase latin, digits, _ or -")
        return value


class PlatformSettings(BaseModel):
    # Plugin parameters (e.g. dzen.editor_url) vary per publisher, so extra keys are allowed here.
    model_config = ConfigDict(extra="allow")

    enabled: bool = True


class Feed(_Strict):
    name: str
    url: str
    id: Optional[str] = None
    category: Optional[str] = None


class RadarSettings(_Strict):
    feeds: List[Feed] = Field(default_factory=list)
    relevant_keywords: List[str] = Field(default_factory=list)
    noise_keywords: List[str] = Field(default_factory=list)


class DeploySettings(_Strict):
    server_url: Optional[str] = None


class GenerationSettings(_Strict):
    # No `generation` section = mode template: the deterministic generators, no LLM calls.
    mode: Literal["template", "llm"] = "template"
    model: Optional[str] = None  # None → settings.llm_model
    temperature: float = Field(0.7, ge=0, le=2)
    max_tokens: int = Field(4096, ge=1)
    review_required: bool = True  # llm articles are created as draft and published after approve
    language: str = "ru"


class VoiceForms:
    """Pronoun helpers for templates: {{ v.I }}, {{ v.my }}, {{ v.we_or_I }} — no `if kind` in templates."""

    def __init__(self, plural: bool):
        self.plural = plural
        self.I = "мы" if plural else "я"
        self.we_or_I = self.I
        self.me = "нас" if plural else "меня"
        self.to_me = "нам" if plural else "мне"
        self.my = "наш" if plural else "мой"
        self.my_f = "наша" if plural else "моя"
        self.my_n = "наше" if plural else "моё"
        self.my_pl = "наши" if plural else "мои"

    def __repr__(self) -> str:
        return f"VoiceForms(plural={self.plural})"


class Profile(_Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    kind: Literal["person", "company"]
    name: str
    display_name: Optional[str] = None
    aliases: List[str] = Field(default_factory=list)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    seo: SeoSettings
    campaign_id: str
    telegram: TelegramSettings
    offer_tiers: List[OfferTier] = Field(default_factory=list)
    lead_magnets: List[LeadMagnet] = Field(default_factory=list)
    default_lead_magnet: Optional[str] = None
    platforms: Dict[str, PlatformSettings] = Field(default_factory=dict)
    radar: RadarSettings = Field(default_factory=RadarSettings)
    templates_dir: Optional[Path] = None
    deploy: DeploySettings = Field(default_factory=DeploySettings)
    generation: GenerationSettings = Field(default_factory=GenerationSettings)

    _source_path: Optional[Path] = PrivateAttr(default=None)

    @field_validator("campaign_id")
    @classmethod
    def _check_campaign_id(cls, value: str) -> str:
        if not re.match(r"^[A-Za-z0-9_-]{1,64}$", value):
            raise ValueError("campaign_id must be latin letters, digits, _ or -")
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> "Profile":
        if self.display_name is None:
            self.display_name = self.name
        _require_unique([t.id for t in self.offer_tiers], "offer_tiers id")
        slugs = [m.slug for m in self.lead_magnets]
        _require_unique(slugs, "lead_magnets slug")
        if self.default_lead_magnet is not None and self.default_lead_magnet not in slugs:
            raise ValueError(f"default_lead_magnet '{self.default_lead_magnet}' is not among lead_magnets slugs {slugs}")
        return self

    @property
    def slug(self) -> str:
        return self._source_path.stem if self._source_path else ""

    @property
    def source_path(self) -> Optional[Path]:
        return self._source_path

    @property
    def is_example(self) -> bool:
        return self.slug.startswith(EXAMPLE_PREFIX)

    @property
    def plural_voice(self) -> bool:
        if self.voice.pronoun == "auto":
            return self.kind == "company"
        return self.voice.pronoun == "мы"

    @property
    def v(self) -> VoiceForms:
        return VoiceForms(self.plural_voice)

    @property
    def templates_path(self) -> Optional[Path]:
        """Override templates: explicit templates_dir (relative to the profile file) or profiles/<name>/templates."""
        base = self._source_path.parent if self._source_path else PROFILES_DIR
        if self.templates_dir is not None:
            path = self.templates_dir if self.templates_dir.is_absolute() else base / self.templates_dir
            return path.resolve()
        if self._source_path:
            candidate = base / self.slug / "templates"
            if candidate.is_dir():
                return candidate.resolve()
        return None

    def lead_magnet(self, slug: Optional[str] = None) -> Optional[LeadMagnet]:
        wanted = slug or self.default_lead_magnet
        for magnet in self.lead_magnets:
            if magnet.slug == wanted:
                return magnet
        return None

    def offer_tier(self, tier_id: str) -> Optional[OfferTier]:
        for tier in self.offer_tiers:
            if tier.id == tier_id:
                return tier
        return None

    def platform_enabled(self, key: str) -> bool:
        # Same rule as publishers.registry.enabled(): no/empty platforms section -> every
        # platform is on; with a section, an unlisted platform is off.
        if not self.platforms:
            return True
        spec = self.platforms.get(key)
        return spec is not None and spec.enabled


def _require_unique(values: List[str], what: str) -> None:
    seen = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {what}: '{value}'")
        seen.add(value)


def load_profile(path: Path) -> Profile:
    path = Path(path)
    if not path.is_file():
        raise ProfileError(f"Profile file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ProfileError(f"Profile {path}: invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ProfileError(f"Profile {path}: expected a mapping at the top level")
    try:
        profile = Profile.model_validate(data)
    except ValidationError as e:
        lines = [f"Profile {path} does not match schema v{SCHEMA_VERSION}:"]
        for err in e.errors():
            loc = ".".join(str(part) for part in err["loc"]) or "<root>"
            lines.append(f"  {loc}: {err['msg']}")
        raise ProfileError("\n".join(lines)) from e
    profile._source_path = path.resolve()
    return profile


def _is_example(path: Path) -> bool:
    return path.stem.startswith(EXAMPLE_PREFIX)


def resolve_profile_path(spec: Optional[str] = None, profiles_dir: Optional[Path] = None) -> Path:
    """spec (or SERM_PROFILE) → path; else the single non-example profile; else ProfileError."""
    profiles_dir = Path(profiles_dir) if profiles_dir else PROFILES_DIR
    spec = spec if spec is not None else os.getenv("SERM_PROFILE")
    spec = (spec or "").strip()

    if spec:
        as_path = Path(spec).expanduser()
        if as_path.suffix in (".yaml", ".yml") or os.sep in spec:
            if not as_path.is_absolute():
                as_path = (BASE_DIR / as_path) if not as_path.exists() else as_path
            if not as_path.is_file():
                raise ProfileError(f"SERM_PROFILE={spec}: file not found ({as_path})")
            return as_path.resolve()
        by_name = profiles_dir / f"{spec}.yaml"
        if not by_name.is_file():
            known = sorted(p.stem for p in profiles_dir.glob("*.yaml"))
            raise ProfileError(f"SERM_PROFILE={spec}: no {by_name}. Available: {', '.join(known) or 'none'}")
        return by_name.resolve()

    own = sorted(p for p in profiles_dir.glob("*.yaml") if not _is_example(p))
    if len(own) == 1:
        return own[0].resolve()
    if len(own) > 1:
        names = ", ".join(p.stem for p in own)
        raise ProfileError(f"Several profiles in {profiles_dir} ({names}) — choose one with SERM_PROFILE=<name>")

    raise ProfileError(
        f"No own profile in {profiles_dir}. Create one: `python cli.py init`, or "
        f"`cp profiles/example-person.yaml profiles/<name>.yaml` (example-company.yaml for a company) and edit it. "
        f"To run on an example as is, set SERM_PROFILE=example-person explicitly."
    )


def _template_files(path: Path):
    return (p for pattern in ("*.j2", "*.yaml") for p in path.rglob(pattern) if p.is_file())


def missing_own_templates(profile: Profile) -> Optional[str]:
    """Why a real (non-example) installation has no override templates dir, or None if it has one.
    Without it every text silently falls back to the neutral built-in scaffold."""
    if profile.is_example:
        return None
    path = profile.templates_path
    # an empty dir (partial rsync, mkdir "to make the deploy pass") is the same silent scaffold as no dir
    if path is not None and path.is_dir() and any(_template_files(path)):
        return None
    expected = path or (profile.source_path.parent / profile.slug / "templates" if profile.source_path else None)
    return (f"profile {profile.slug or '?'} has no own templates (no *.j2 / *.yaml in {expected}): bot, posts and articles "
            f"would speak with the built-in neutral scaffold. Copy profiles/{profile.slug}/templates next to the profile.")


def profile_summary(profile: Profile) -> str:
    """One line for the startup log: which brand the process speaks as, from which file and templates."""
    templates = profile.templates_path
    templates = templates if templates is not None and templates.is_dir() else "none (built-in)"
    return (
        f"profile={profile.slug or '?'} kind={profile.kind} name={profile.display_name!r} "
        f"channel={profile.telegram.channel} file={profile.source_path} templates={templates}"
    )


@lru_cache(maxsize=None)
def get_profile(spec: Optional[str] = None) -> Profile:
    """Process-wide profile. Tests: get_profile.cache_clear() after changing SERM_PROFILE."""
    return load_profile(resolve_profile_path(spec))
