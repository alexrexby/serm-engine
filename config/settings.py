"""
Configuration settings for SERM Distribution & Fast Indexing Engine.

All environment is read through one `Settings` class. Priority: process env > .env file.
Every historical env name still works (first name in each AliasChoices is canonical,
the rest are aliases). Module constants below are computed from Settings, so existing
`from config.settings import X` imports keep working.
"""
import logging
import os
import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import ClassVar, Dict, List, Optional

from pydantic import AliasChoices, Field, PrivateAttr, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from core import envfile

BASE_DIR = Path(__file__).resolve().parent.parent
# SERM_ENV_FILE (process env only, read at import): one env file path for settings, admin and cli;
# tests point it at a missing file before importing the project
ENV_FILE = Path(os.environ.get("SERM_ENV_FILE") or BASE_DIR / ".env")

DEFAULT_LLM_BASE_URL = "https://api.polza.ai/v1"
DEFAULT_LLM_MODEL = "anthropic/claude-sonnet-5.5"

BOT_TOKEN_RE = re.compile(r"^\d+:[\w-]{30,}$")
CHANNEL_RE = re.compile(r"^(@[A-Za-z0-9_]+|-100\d+)$")
CONTACT_RE = re.compile(r"^@[A-Za-z0-9_]+$")
VK_OWNER_RE = re.compile(r"^-?\d+$")
URL_RE = re.compile(r"^https?://")

logger = logging.getLogger("Settings")

# Keys whose empty value in the .env file is kept as "" (old readers did env.get(KEY, default);
# LLM_MAX_COST_PER_RUN= means "no limit"); every other empty value counts as unset
KEEP_EMPTY_IN_FILE = {"OPP_CHANNEL", "OPP_CONTACT", "SERM_ACTIVE_CAMPAIGN", "LLM_MAX_COST_PER_RUN"}
# LLM_MAX_COST_PER_RUN values that switch the budget off
NO_LIMIT_VALUES = {"", "none"}


class SettingsError(ValueError):
    """Invalid configuration value; the message names the env variable."""


class EnvFileSource(PydanticBaseSettingsSource):
    """
    Reads the .env file through core.envfile (same parsing as the historical readers).
    Public source API only: per field the first alias present in the file wins and is handed
    over under the canonical (first) name, the same keys the process env source produces,
    so "process env > file" holds per field.
    """

    def __init__(self, settings_cls, env_path: Optional[Path]):
        self.env_path = env_path
        super().__init__(settings_cls)

    def get_field_value(self, field, field_name: str):
        # required by the base class; __call__ below does the whole file at once
        return None, field_name, False

    def __call__(self) -> Dict[str, str]:
        data = envfile.read(self.env_path) if self.env_path else {}
        data = {k: v for k, v in data.items() if v != "" or k in KEEP_EMPTY_IN_FILE}
        res: Dict[str, str] = {}
        for field_name in self.settings_cls.model_fields:
            names = _env_names(field_name)
            found = next((n for n in names if n in data), None)
            if found is not None:
                res[names[0]] = data[found]
        return res


def _env(*names: str) -> AliasChoices:
    return AliasChoices(*names)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True, env_ignore_empty=True, extra="ignore")

    # .env location; load_settings(env_file=...) swaps it (tests use temp files)
    env_path: ClassVar[Optional[Path]] = ENV_FILE
    _problems: List[str] = PrivateAttr(default_factory=list)

    # Telegram: lead-magnet bot (falls back to the indexing bot token, as before)
    bot_token: SecretStr = Field(SecretStr(""), validation_alias=_env("OPP_BOT_TOKEN", "SERM_TG_BOT_TOKEN"),
                                 description="Telegram bot token from @BotFather (lead-magnet bot)")
    channel: Optional[str] = Field(None, validation_alias=_env("OPP_CHANNEL"),
                                   description="Channel users must subscribe to: @name or -100... id")
    contact: Optional[str] = Field(None, validation_alias=_env("OPP_CONTACT"),
                                   description="Contact for inquiries: @username")
    active_campaign: Optional[str] = Field(None, validation_alias=_env("SERM_ACTIVE_CAMPAIGN"),
                                           description="Campaign id whose lead magnet the bot serves")

    # Fast indexing
    index_bot_token: SecretStr = Field(SecretStr(""), validation_alias=_env("SERM_TG_BOT_TOKEN"),
                                       description="Bot token for posting to the indexing channel")
    index_channel_id: str = Field("", validation_alias=_env("SERM_TG_CHANNEL_ID"),
                                  description="Public channel for fast indexing: @name")
    vk_token: SecretStr = Field(SecretStr(""), validation_alias=_env("SERM_VK_TOKEN"),
                                description="VK API access token for wall posts")
    vk_owner_id: str = Field("", validation_alias=_env("SERM_VK_OWNER_ID"),
                             description="VK user or group id (group: -123456)")

    # LLM provider (OpenAI-compatible)
    llm_api_key: SecretStr = Field(SecretStr(""), validation_alias=_env("POLZA_API_KEY", "LLM_API_KEY"),
                                   description="LLM provider API key")
    llm_base_url: str = Field(DEFAULT_LLM_BASE_URL, validation_alias=_env("POLZA_BASE_URL", "LLM_BASE_URL"),
                              description="OpenAI-compatible API base URL")
    llm_model: str = Field(DEFAULT_LLM_MODEL, validation_alias=_env("SERM_LLM_MODEL", "LLM_MODEL"),
                           description="Default model id")
    llm_timeout_s: int = Field(120, ge=1, validation_alias=_env("LLM_TIMEOUT"),
                               description="LLM request timeout, seconds")
    llm_max_retries: int = Field(3, ge=0, le=10, validation_alias=_env("LLM_MAX_RETRIES"),
                                 description="Retries on 429/5xx/timeouts")
    llm_price_in: Optional[Decimal] = Field(None, ge=0, validation_alias=_env("LLM_PRICE_INPUT_PER_MTOK"),
                                            description="Price per 1M input tokens, provider billing currency")
    llm_price_out: Optional[Decimal] = Field(None, ge=0, validation_alias=_env("LLM_PRICE_OUTPUT_PER_MTOK"),
                                             description="Price per 1M output tokens, provider billing currency")
    llm_max_cost_per_run: Optional[Decimal] = Field(Decimal("300"), ge=0, validation_alias=_env("LLM_MAX_COST_PER_RUN"),
                                                    description="Budget per generation run in the provider billing "
                                                                "currency (Polza: RUB, many others: USD); empty or "
                                                                "none = no limit. Works only if the provider returns "
                                                                "usage.cost or LLM_PRICE_* are set")

    # Admin panel & alerts
    admin_token: SecretStr = Field(SecretStr(""), validation_alias=_env("ADMIN_TOKEN", "ADMIN_PASSWORD"),
                                   description="Admin panel token; empty = generated on first start")
    admin_tg_id: Optional[int] = Field(None, validation_alias=_env("ADMIN_TG_ID", "TELEGRAM_ADMIN_ID"),
                                       description="Telegram user id that receives system alerts")
    admin_port: int = Field(8180, ge=1, le=65535, validation_alias=_env("ADMIN_PORT"),
                            description="Admin panel port")
    admin_host: str = Field("127.0.0.1", validation_alias=_env("ADMIN_HOST"),
                            description="Admin panel bind address (0.0.0.0 = all interfaces; Docker sets it)")
    autopilot_enabled: str = Field("", validation_alias=_env("AUTOPILOT_ENABLED"),
                                   description="true/false overrides the autopilot state file")

    # Installation
    profile: Optional[str] = Field(None, validation_alias=_env("SERM_PROFILE"),
                                   description="Profile name or path to profiles/<name>.yaml")
    data_dir: Path = Field(BASE_DIR / "data", validation_alias=_env("SERM_DATA_DIR"),
                           description="Data directory (db, sessions, exports)")
    deploy_host: Optional[str] = Field(None, validation_alias=_env("SERM_DEPLOY_HOST"),
                                       description="sync-sessions: user@host of the server")
    deploy_key: Optional[str] = Field(None, validation_alias=_env("SERM_DEPLOY_KEY"),
                                      description="sync-sessions: path to the SSH key")
    deploy_dir: Optional[str] = Field(None, validation_alias=_env("SERM_DEPLOY_DIR"),
                                      description="sync-sessions: install directory on the server")

    @field_validator("llm_max_cost_per_run", mode="before")
    @classmethod
    def _no_limit(cls, value):
        if isinstance(value, str) and value.strip().lower() in NO_LIMIT_VALUES:
            return None
        return value

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings,
                                   file_secret_settings):
        # process env first, then the .env file via core.envfile (python-dotenv is not used)
        return init_settings, env_settings, EnvFileSource(settings_cls, cls.env_path)

    @model_validator(mode="wrap")
    @classmethod
    def _lenient(cls, data, handler):
        # Old code accepted any value: a bad format is a warning, not a crash (strict mode raises).
        # Sources hand over a dict keyed by the canonical env name.
        problems = []
        if isinstance(data, dict) and data.get("ADMIN_TG_ID") not in (None, ""):
            try:
                int(str(data["ADMIN_TG_ID"]).strip())
            except ValueError:
                # alerts treated a non-numeric id as "not set"
                problems.append("ADMIN_TG_ID / TELEGRAM_ADMIN_ID: expected numeric Telegram user id, ignored")
                data = {k: v for k, v in data.items() if k != "ADMIN_TG_ID"}
        try:
            obj = handler(data)
        except ValidationError as e:
            # A value that cannot be parsed at all (LLM_PRICE_*=1,5, LLM_MAX_COST_PER_RUN=300р) is dropped:
            # the field takes its default and the problem is reported. Fresh reads at runtime
            # must not raise on a hand-edited .env; strict mode (doctor) still fails on it.
            if not isinstance(data, dict):
                raise
            bad = set()
            for err in e.errors():
                loc = str(err["loc"][0]) if err["loc"] else ""
                bad.add(loc)
                if _canonical(loc) == "LLM_MAX_COST_PER_RUN":
                    # fail closed on money: a broken budget must not open up to the default
                    problems.append(f"{_names_for(loc)}: {err['msg']}; budget 0, LLM runs are refused")
                else:
                    problems.append(f"{_names_for(loc)}: {err['msg']}; default used")
            data = {k: v for k, v in data.items() if k not in bad and _canonical(k) not in bad}
            if any(_canonical(loc) == "LLM_MAX_COST_PER_RUN" for loc in bad):
                data["LLM_MAX_COST_PER_RUN"] = Decimal(0)
            obj = handler(data)
        obj._problems = problems + obj._format_problems()
        return obj

    def _format_problems(self) -> List[str]:
        checks = [
            ("bot_token", self.bot_token.get_secret_value(), BOT_TOKEN_RE, "expected <digits>:<30+ chars>"),
            ("index_bot_token", self.index_bot_token.get_secret_value(), BOT_TOKEN_RE,
             "expected <digits>:<30+ chars>"),
            ("channel", self.channel, CHANNEL_RE, "expected @channel_name or -100... chat id"),
            ("contact", self.contact, CONTACT_RE, "expected @username"),
            ("vk_owner_id", self.vk_owner_id, VK_OWNER_RE, "expected numeric id, groups are negative (-123456)"),
            ("llm_base_url", self.llm_base_url, URL_RE, "expected http(s):// URL"),
        ]
        return [f"{' / '.join(_env_names(field))}: {msg}" for field, value, rx, msg in checks
                if value and not rx.match(value)]

    def problems(self) -> List[str]:
        """Invalid formats found at load; values are kept as is and never put into the text."""
        return list(self._problems)

    def warnings(self) -> List[str]:
        """Non-fatal problems: the system starts without these, as before."""
        res = self.problems()
        if not self.bot_token.get_secret_value():
            res.append("OPP_BOT_TOKEN is not set: Telegram bot is disabled")
        if self.admin_tg_id is None:
            res.append("ADMIN_TG_ID is not set: system alerts are not delivered")
        token = self.admin_token.get_secret_value()
        if token and len(token) < 16:
            res.append("ADMIN_TOKEN is shorter than 16 characters")
        if self.admin_host == "0.0.0.0":
            res.append("ADMIN_HOST=0.0.0.0: admin panel listens on all interfaces")
        return res


def _env_names(field_name: str) -> List[str]:
    alias = Settings.model_fields[field_name].validation_alias
    return [str(c) for c in alias.choices] if isinstance(alias, AliasChoices) else [field_name]


def _canonical(name: str) -> str:
    """Field name or any alias -> canonical env name (error locations use it)."""
    for field in Settings.model_fields:
        names = _env_names(field)
        if name == field or name in names:
            return names[0]
    return name


def _names_for(loc: str) -> str:
    # error locations are canonical env names (field names for init kwargs)
    for field in Settings.model_fields:
        names = _env_names(field)
        if loc == field or loc in names:
            return " / ".join(names)
    return loc


def _format_errors(e: ValidationError) -> str:
    lines = []
    for err in e.errors():
        loc = str(err["loc"][0]) if err["loc"] else "?"
        lines.append(f"{_names_for(loc)}: {err['msg']}")
    return "invalid configuration:\n  " + "\n  ".join(lines)


_logged_problems = set()


def load_settings(env_file: Optional[Path] = None, strict: bool = False) -> Settings:
    """
    Reads env + .env now (no cache): use where values may change at runtime.
    Never raises on a bad value unless strict=True (cli doctor): a format problem keeps the value,
    an unparseable value (ADMIN_PORT=abc, LLM_PRICE_*=1,5) falls back to the default;
    both are logged once.
    """
    cls = Settings
    if env_file is not None:
        cls = type("Settings", (Settings,), {"env_path": Path(env_file), "__module__": __name__})
    try:
        settings = cls()
    except ValidationError as e:
        raise SettingsError(_format_errors(e)) from None
    problems = settings.problems()
    if strict and problems:
        raise SettingsError("invalid configuration:\n  " + "\n  ".join(problems))
    for problem in problems:
        if problem not in _logged_problems:
            _logged_problems.add(problem)
            logger.warning(f"Config: {problem}")
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings snapshot taken at first use."""
    return load_settings()


def get_bot_token() -> str:
    """Lead-magnet bot token: OPP_BOT_TOKEN, else SERM_TG_BOT_TOKEN (env, then .env). Read fresh."""
    return load_settings().bot_token.get_secret_value()


def render_env_example() -> str:
    """Text of .env.example generated from the Settings schema."""
    out = [
        "# SERM Distribution Engine configuration.",
        "# Copy to .env and fill in. Process environment overrides values from .env.",
        "# Generated from config/settings.py: python -m cli env-example > .env.example",
        "# SERM_ENV_FILE (process environment only, not this file): path to another env file; default .env",
        "# in the project root.",
        "",
    ]
    for name, field in Settings.model_fields.items():
        names = _env_names(name)
        out.append(f"# {field.description}")
        if len(names) > 1:
            out.append(f"# aliases: {', '.join(names[1:])}")
        default = field.default
        if isinstance(default, SecretStr) or default is None or name == "data_dir":
            value = ""
        else:
            value = str(default)
        out.append(f"{names[0]}={value}")
        out.append("")
    return "\n".join(out)


_settings = get_settings()

DATA_DIR = _settings.data_dir
SESSIONS_DIR = DATA_DIR / "sessions"
EXPORTS_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "serm.db"

# Ensure directories exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

# Fast Indexing Channels Config
TELEGRAM_BOT_TOKEN = _settings.index_bot_token.get_secret_value()
TELEGRAM_INDEX_CHANNEL_ID = _settings.index_channel_id  # Public indexed channel (@channel_name)
VK_API_ACCESS_TOKEN = _settings.vk_token.get_secret_value()
VK_OWNER_ID = _settings.vk_owner_id  # VK user or group ID (-123456)

# Platform Profiles & Rules
PLATFORMS_CONFIG = {
    "vc": {
        "name": "VC.ru",
        "url": "https://vc.ru",
        "type": "direct_ugc",
        "min_chars": 2500,
        "max_chars": 20000,
        "allow_markdown": True,
        "fast_index_minutes": 15,
        "session_dir": SESSIONS_DIR / "vc_profile",
    },
    "spark": {
        "name": "Spark.ru",
        "url": "https://spark.ru",
        "type": "direct_ugc",
        "min_chars": 2000,
        "max_chars": 15000,
        "allow_markdown": True,
        "fast_index_minutes": 60,
        "session_dir": SESSIONS_DIR / "spark_profile",
    },
    "dzen": {
        "name": "Дзен (Dzen.ru)",
        "url": "https://dzen.ru/profile/editor",
        "type": "direct_ugc",
        "min_chars": 1500,
        "max_chars": 12000,
        "allow_markdown": False,
        "fast_index_minutes": 30,
        "session_dir": SESSIONS_DIR / "dzen_profile",
    },
    "executive": {
        "name": "E-xecutive.ru",
        "url": "https://www.e-xecutive.ru",
        "type": "moderated_ugc",
        "min_chars": 5000,
        "max_chars": 14000,
        "allow_markdown": False,
        "moderation_days": 10,
        "session_dir": SESSIONS_DIR / "executive_profile",
    },
    "tbank": {
        "name": "Т-Банк Секреты",
        "url": "https://secrets.tbank.ru",
        "type": "editorial_pitch",
        "telegram_bot": "@bs_community_bot",
        "min_chars": 6000,
        "moderation_days": 7,
    },
    "biz360": {
        "name": "Бизнес-портал Biz360.ru",
        "url": "https://biz360.ru/about/",
        "type": "editorial_pitch",
        "min_chars": 5000,
        "moderation_days": 7,
    },
    "rb": {
        "name": "RB.RU (Колонки)",
        "url": "https://rb.ru/blogers/",
        "type": "editorial_pitch",
        "tilda_form_id": "form764023213",
        "min_chars": 5000,
        "moderation_days": 7,
    },
}
