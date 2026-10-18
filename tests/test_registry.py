"""
publishers/registry.py: legacy PLATFORMS_CONFIG view, enabled(profile), lazy get_publisher().
"""
import sys
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import publishers.registry as registry  # noqa: E402

ALL_KEYS = ["vc", "spark", "dzen", "executive", "tbank", "biz360", "rb"]

# Mapping of the legacy get_publisher() (core/admin.py, core/autopilot.py).
LEGACY_CLASSES = {
    "vc": "VCPublisher",
    "spark": "SparkPublisher",
    "dzen": "DzenPublisher",
    "executive": "ExecutivePublisher",
    "tbank": "EditorialPitcher",
    "biz360": "EditorialPitcher",
    "rb": "EditorialPitcher",
}


def test_keys_unchanged():
    assert list(registry.PLATFORMS) == ALL_KEYS


def test_platforms_config_matches_settings():
    from config.settings import PLATFORMS_CONFIG, SESSIONS_DIR
    assert registry.platforms_config(SESSIONS_DIR) == PLATFORMS_CONFIG


def test_factories_resolve_to_legacy_classes():
    for key, spec in registry.PLATFORMS.items():
        module_name, class_name = spec.factory.split(":")
        assert class_name == LEGACY_CLASSES[key]
        assert isinstance(getattr(import_module(module_name), class_name), type)


def test_enabled_defaults_to_all():
    assert registry.enabled(None) == ALL_KEYS
    assert registry.enabled({}) == ALL_KEYS
    assert registry.enabled({"platforms": None}) == ALL_KEYS


def test_enabled_respects_profile():
    profile = {"platforms": {"dzen": {"enabled": False}, "vc": {"enabled": True}, "spark": {}}}
    assert registry.enabled(profile) == ["vc", "spark"]
    obj = SimpleNamespace(platforms=SimpleNamespace(spark=SimpleNamespace(enabled=False),
                                                    rb=SimpleNamespace(enabled=True)))
    assert registry.enabled(obj) == ["rb"]


def test_enabled_unlisted_platform_is_disabled():
    # An empty section is "not configured" (Profile.platforms defaults to {}): all on.
    assert registry.enabled({"platforms": {}}) == list(registry.PLATFORMS)
    assert registry.enabled({"platforms": {"vc": {"enabled": True}}}) == ["vc"]


class _FakePublisher:
    def __init__(self, *args):
        self.args = args


def test_get_publisher_builds_from_factory(monkeypatch):
    spec = registry.PlatformSpec(key="fake", name="Fake", url="https://example.com", type="editorial_pitch",
                                 factory=f"{__name__}:_FakePublisher", factory_args=("fake",))
    monkeypatch.setitem(registry.PLATFORMS, "fake", spec)
    publisher = registry.get_publisher("fake")
    assert isinstance(publisher, _FakePublisher) and publisher.args == ("fake",)


def test_get_publisher_editorial():
    from publishers.editorial_pitcher import EditorialPitcher
    publisher = registry.get_publisher("rb")
    assert isinstance(publisher, EditorialPitcher) and publisher.platform_name == "rb"


def test_get_publisher_unknown():
    with pytest.raises(ValueError, match="Unknown platform: nope"):
        registry.get_publisher("nope")


def test_enabled_real_profile_without_platforms_section(tmp_path):
    """Profile.platforms defaults to {} — a profile without the section must enable all."""
    import yaml
    from core.profile import load_profile

    src = Path(__file__).resolve().parent.parent / "profiles" / "example-person.yaml"
    data = yaml.safe_load(src.read_text(encoding="utf-8"))
    data.pop("platforms", None)
    f = tmp_path / "noplat.yaml"
    f.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    profile = load_profile(f)
    assert registry.enabled(profile) == list(registry.PLATFORMS)
    assert all(profile.platform_enabled(k) for k in registry.PLATFORMS)
