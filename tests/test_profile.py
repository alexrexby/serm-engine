"""
Profile schema and resolution (core/profile.py, plan Contract 2).
Uses only example profiles and tmp dirs — the owner's profile is never required.
"""
import sys
import logging
import shutil
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.profile import ProfileError, load_profile, profile_summary, resolve_profile_path  # noqa: E402

PROFILES = PROJECT_ROOT / "profiles"
EXAMPLES = ["example-person", "example-company"]


def _write(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _example_data(name: str = "example-person") -> dict:
    return yaml.safe_load((PROFILES / f"{name}.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def profiles_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("SERM_PROFILE", raising=False)
    d = tmp_path / "profiles"
    d.mkdir()
    for name in EXAMPLES:
        shutil.copy(PROFILES / f"{name}.yaml", d / f"{name}.yaml")
    return d


@pytest.mark.parametrize("name", EXAMPLES)
def test_examples_validate(name):
    profile = load_profile(PROFILES / f"{name}.yaml")
    assert profile.slug == name and profile.is_example
    assert profile.display_name
    assert profile.lead_magnet().slug == profile.default_lead_magnet
    assert profile.templates_path is None


def test_voice_by_kind():
    person = load_profile(PROFILES / "example-person.yaml")
    company = load_profile(PROFILES / "example-company.yaml")
    assert person.kind == "person" and (person.v.I, person.v.my, person.v.we_or_I) == ("я", "мой", "я")
    assert company.kind == "company" and (company.v.I, company.v.my, company.v.we_or_I) == ("мы", "наш", "мы")


def test_explicit_pronoun_overrides_kind(tmp_path):
    data = _example_data("example-company")
    data["voice"]["pronoun"] = "я"
    assert load_profile(_write(tmp_path / "p.yaml", data)).v.I == "я"


def test_display_name_defaults_to_name(tmp_path):
    data = _example_data()
    del data["display_name"]
    assert load_profile(_write(tmp_path / "p.yaml", data)).display_name == data["name"]


def test_platform_enabled_defaults_to_true(tmp_path):
    data = _example_data()
    del data["platforms"]
    profile = load_profile(_write(tmp_path / "p.yaml", data))
    assert all(profile.platform_enabled(k) for k in ("vc", "dzen", "rb"))
    example = load_profile(PROFILES / "example-person.yaml")
    assert example.platform_enabled("vc") and not example.platform_enabled("dzen")
    assert example.platforms["dzen"].editor_url.startswith("https://dzen.ru/")


@pytest.mark.parametrize("mutate, where", [
    (lambda d: d.update(telegarm=d.pop("telegram")), "telegarm"),          # typo in a top-level key
    (lambda d: d["voice"].update(tone_of_voice="x"), "voice.tone_of_voice"),  # typo in a nested key
    (lambda d: d.update(kind="startup"), "kind"),
    (lambda d: d.update(schema_version=2), "schema_version"),
    (lambda d: d["telegram"].update(channel="example_channel"), "telegram.channel"),
    (lambda d: d["telegram"].update(contact="https://t.me/x"), "telegram.contact"),
    (lambda d: d.update(default_lead_magnet="missing"), "default_lead_magnet"),
    (lambda d: d["lead_magnets"].append(dict(d["lead_magnets"][0])), "duplicate lead_magnets slug"),
    (lambda d: d["lead_magnets"][0].update(slug="Bad Slug"), "lead_magnets.0.slug"),
    (lambda d: d.pop("campaign_id"), "campaign_id"),
])
def test_broken_profile_is_rejected(tmp_path, mutate, where):
    data = _example_data()
    mutate(data)
    with pytest.raises(ProfileError) as e:
        load_profile(_write(tmp_path / "broken.yaml", data))
    assert where in str(e.value) and "broken.yaml" in str(e.value)


def test_invalid_yaml_and_missing_file(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("kind: [person\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="invalid YAML"):
        load_profile(bad)
    with pytest.raises(ProfileError, match="not found"):
        load_profile(tmp_path / "nope.yaml")


def test_resolve_without_own_profile_fails_loud(profiles_dir):
    # Examples are never picked silently: on a server without the owner's profile the bot
    # would otherwise speak as "Иван Петров".
    with pytest.raises(ProfileError, match="cli.py init|cp profiles/example"):
        resolve_profile_path(profiles_dir=profiles_dir)


def test_explicit_example_profile_works(profiles_dir, monkeypatch):
    monkeypatch.setenv("SERM_PROFILE", "example-person")
    path = resolve_profile_path(profiles_dir=profiles_dir)
    assert path.name == "example-person.yaml"
    assert load_profile(path).is_example


def test_profile_summary():
    summary = profile_summary(load_profile(PROFILES / "example-company.yaml"))
    for part in ("Ромашка", "company", "@example_company_news", "example-company.yaml"):
        assert part in summary


def test_profile_summary_shows_templates(profiles_dir):
    assert "templates=none" in profile_summary(load_profile(PROFILES / "example-company.yaml"))
    _write(profiles_dir / "acme.yaml", _example_data())
    (profiles_dir / "acme" / "templates").mkdir(parents=True)
    summary = profile_summary(load_profile(profiles_dir / "acme.yaml"))
    assert f"templates={(profiles_dir / 'acme' / 'templates').resolve()}" in summary


def test_empty_own_templates_dir_is_missing(profiles_dir):
    # N5: an empty dir is the same silent neutral scaffold as no dir
    from core.profile import missing_own_templates
    _write(profiles_dir / "acme.yaml", _example_data())
    (profiles_dir / "acme" / "templates" / "bot").mkdir(parents=True)
    assert "no own templates" in missing_own_templates(load_profile(profiles_dir / "acme.yaml"))
    (profiles_dir / "acme" / "templates" / "bot" / "start.j2").write_text("hi\n", encoding="utf-8")
    assert missing_own_templates(load_profile(profiles_dir / "acme.yaml")) is None


def test_tier_id_fits_telegram_callback_data(tmp_path):
    # callback_data is tier_<id> and Telegram caps it at 64 bytes
    data = _example_data()
    data["offer_tiers"][0]["id"] = "a" * 58
    assert len(f"tier_{load_profile(_write(tmp_path / 'ok.yaml', data)).offer_tiers[0].id}".encode()) == 63
    data["offer_tiers"][0]["id"] = "a" * 60
    with pytest.raises(ProfileError, match="offer_tiers.0.id"):
        load_profile(_write(tmp_path / "long.yaml", data))


def test_resolve_picks_single_own_profile(profiles_dir):
    _write(profiles_dir / "acme.yaml", _example_data("example-company"))
    assert resolve_profile_path(profiles_dir=profiles_dir).name == "acme.yaml"
    assert load_profile(resolve_profile_path(profiles_dir=profiles_dir)).slug == "acme"


def test_resolve_ambiguous_own_profiles_requires_choice(profiles_dir):
    _write(profiles_dir / "acme.yaml", _example_data())
    _write(profiles_dir / "beta.yaml", _example_data())
    with pytest.raises(ProfileError, match="SERM_PROFILE"):
        resolve_profile_path(profiles_dir=profiles_dir)
    assert resolve_profile_path("beta", profiles_dir=profiles_dir).name == "beta.yaml"


def test_resolve_serm_profile_env_name_and_path(profiles_dir, tmp_path, monkeypatch):
    _write(profiles_dir / "acme.yaml", _example_data())
    monkeypatch.setenv("SERM_PROFILE", "example-company")
    assert resolve_profile_path(profiles_dir=profiles_dir).name == "example-company.yaml"
    elsewhere = _write(tmp_path / "custom.yaml", _example_data())
    monkeypatch.setenv("SERM_PROFILE", str(elsewhere))
    assert resolve_profile_path(profiles_dir=profiles_dir) == elsewhere.resolve()


def test_resolve_unknown_serm_profile_fails_loud(profiles_dir, monkeypatch):
    monkeypatch.setenv("SERM_PROFILE", "nobody")
    with pytest.raises(ProfileError, match="Available: example-company, example-person"):
        resolve_profile_path(profiles_dir=profiles_dir)
    monkeypatch.setenv("SERM_PROFILE", "/no/such/profile.yaml")
    with pytest.raises(ProfileError, match="file not found"):
        resolve_profile_path(profiles_dir=profiles_dir)


def test_no_profiles_at_all_is_explained(tmp_path, monkeypatch):
    monkeypatch.delenv("SERM_PROFILE", raising=False)
    with pytest.raises(ProfileError, match="No own profile"):
        resolve_profile_path(profiles_dir=tmp_path)


def test_override_templates_dir(profiles_dir):
    _write(profiles_dir / "acme.yaml", _example_data())
    (profiles_dir / "acme" / "templates").mkdir(parents=True)
    assert load_profile(profiles_dir / "acme.yaml").templates_path == (profiles_dir / "acme" / "templates").resolve()
    data = _example_data()
    data["templates_dir"] = "custom_tpl"
    assert load_profile(_write(profiles_dir / "beta.yaml", data)).templates_path == (profiles_dir / "custom_tpl").resolve()


def test_installed_own_profile_validates_if_present():
    """On the owner's machine the real profile must match the schema too (skipped in public CI)."""
    own = [p for p in PROFILES.glob("*.yaml") if not p.stem.startswith("example")]
    if not own:
        pytest.skip("no own profile installed")
    for path in own:
        profile = load_profile(path)
        assert not profile.is_example and profile.lead_magnet() is not None
