"""
Platform registry: single source of platform metadata and publisher factories.

Removing a platform = delete its publisher module + its line in PLATFORMS (no cascade:
publishers are imported lazily by `get_publisher`).
Keys are the session directory names (`data/sessions/<key>_profile`) — never rename them
on a live installation, or saved cookies are lost.
"""
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class PlatformSpec:
    key: str
    name: str
    url: str
    type: str  # direct_ugc | moderated_ugc | editorial_pitch
    factory: str  # "module.path:ClassName"
    limits: Dict[str, Any] = field(default_factory=dict)  # min_chars, max_chars, allow_markdown, ...
    factory_args: tuple = ()
    has_session: bool = True  # browser session in data/sessions/<key>_profile


PLATFORMS: Dict[str, PlatformSpec] = {
    "vc": PlatformSpec(
        key="vc",
        name="VC.ru",
        url="https://vc.ru",
        type="direct_ugc",
        factory="publishers.vc_publisher:VCPublisher",
        limits={"min_chars": 2500, "max_chars": 20000, "allow_markdown": True, "fast_index_minutes": 15},
    ),
    "spark": PlatformSpec(
        key="spark",
        name="Spark.ru",
        url="https://spark.ru",
        type="direct_ugc",
        factory="publishers.spark_publisher:SparkPublisher",
        limits={"min_chars": 2000, "max_chars": 15000, "allow_markdown": True, "fast_index_minutes": 60},
    ),
    "dzen": PlatformSpec(
        key="dzen",
        name="Дзен (Dzen.ru)",
        url="https://dzen.ru/profile/editor",
        type="direct_ugc",
        factory="publishers.dzen_publisher:DzenPublisher",
        limits={"min_chars": 1500, "max_chars": 12000, "allow_markdown": False, "fast_index_minutes": 30},
    ),
    "executive": PlatformSpec(
        key="executive",
        name="E-xecutive.ru",
        url="https://www.e-xecutive.ru",
        type="moderated_ugc",
        factory="publishers.executive_publisher:ExecutivePublisher",
        limits={"min_chars": 5000, "max_chars": 14000, "allow_markdown": False, "moderation_days": 10},
    ),
    "tbank": PlatformSpec(
        key="tbank",
        name="Т-Банк Секреты",
        url="https://secrets.tbank.ru",
        type="editorial_pitch",
        factory="publishers.editorial_pitcher:EditorialPitcher",
        factory_args=("tbank",),
        limits={"telegram_bot": "@bs_community_bot", "min_chars": 6000, "moderation_days": 7},
        has_session=False,
    ),
    "biz360": PlatformSpec(
        key="biz360",
        name="Бизнес-портал Biz360.ru",
        url="https://biz360.ru/about/",
        type="editorial_pitch",
        factory="publishers.editorial_pitcher:EditorialPitcher",
        factory_args=("biz360",),
        limits={"min_chars": 5000, "moderation_days": 7},
        has_session=False,
    ),
    "rb": PlatformSpec(
        key="rb",
        name="RB.RU (Колонки)",
        url="https://rb.ru/blogers/",
        type="editorial_pitch",
        factory="publishers.editorial_pitcher:EditorialPitcher",
        factory_args=("rb",),
        limits={"tilda_form_id": "form764023213", "min_chars": 5000, "moderation_days": 7},
        has_session=False,
    ),
}


def platforms_config(sessions_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Legacy dict view (shape of config.settings.PLATFORMS_CONFIG)."""
    config = {}
    for key, spec in PLATFORMS.items():
        entry = {"name": spec.name, "url": spec.url, "type": spec.type, **spec.limits}
        if spec.has_session:
            entry["session_dir"] = Path(sessions_dir) / f"{key}_profile"
        config[key] = entry
    return config


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def enabled(profile: Optional[Any] = None) -> List[str]:
    """Platform keys enabled by the profile.

    No profile / no `platforms` section -> all enabled (legacy behaviour). With a section,
    a platform not listed there is disabled; a listed one is enabled unless `enabled: false`.
    """
    section = _get(profile, "platforms")
    # Profile.platforms defaults to {} (not None): an empty section means "not configured".
    if not section:
        return list(PLATFORMS)
    keys = []
    for key in PLATFORMS:
        entry = _get(section, key)
        if entry is None:
            continue
        flag = _get(entry, "enabled")
        if flag is None or flag:
            keys.append(key)
    return keys


def get_publisher(platform: str):
    spec = PLATFORMS.get(platform)
    if spec is None:
        raise ValueError(f"Unknown platform: {platform}")
    module_name, class_name = spec.factory.split(":")
    cls = getattr(import_module(module_name), class_name)
    return cls(*spec.factory_args)
