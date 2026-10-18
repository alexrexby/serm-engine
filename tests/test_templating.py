"""
core/templating.py: profile override templates win over built-in ones (ChoiceLoader order),
StrictUndefined, autoescape only for admin/** and lead_magnet/*.html.j2, deeplink().
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import UndefinedError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import core.templating as templating  # noqa: E402


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    override = tmp_path / "override"
    (builtin / "bot").mkdir(parents=True)
    (override / "bot").mkdir(parents=True)
    (builtin / "bot" / "start.md.j2").write_text("builtin {{ profile.name }}\n", encoding="utf-8")
    (override / "bot" / "start.md.j2").write_text("override {{ profile.name }}\n", encoding="utf-8")
    (builtin / "only_builtin.md.j2").write_text("only builtin\n", encoding="utf-8")
    monkeypatch.setattr(templating, "BUILTIN_TEMPLATES_DIR", builtin)
    return builtin, override


def test_profile_template_overrides_builtin(dirs):
    _, override = dirs
    profile = {"name": "Иван", "templates_dir": str(override)}
    assert templating.render("bot/start.md.j2", profile) == "override Иван\n"


def test_builtin_used_when_profile_has_no_such_template(dirs):
    _, override = dirs
    profile = {"name": "Иван", "templates_dir": str(override)}
    assert templating.render("only_builtin.md.j2", profile) == "only builtin\n"


def test_builtin_used_without_templates_dir(dirs):
    profile = SimpleNamespace(name="ООО Ромашка", templates_dir=None)
    assert templating.render("bot/start.md.j2", profile) == "builtin ООО Ромашка\n"


def test_strict_undefined_raises(dirs):
    builtin, _ = dirs
    (builtin / "typo.md.j2").write_text("{{ profile.nmae }}", encoding="utf-8")
    with pytest.raises(UndefinedError):
        templating.render("typo.md.j2", {"name": "Иван"})


@pytest.mark.parametrize("name, escaped", [
    ("admin/index.html.j2", True),
    ("admin/login.html.j2", True),
    ("lead_magnet/doc.html.j2", True),
    ("lead_magnet/doc.md.j2", False),
    ("bot/start.html.j2", False),
    ("telegram/posts/01-intro.md.j2", False),
    ("partials/offer.html.j2", False),
])
def test_autoescape_only_for_web_html(dirs, name, escaped):
    builtin, _ = dirs
    path = builtin / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{{ value }}", encoding="utf-8")
    expected = "R&amp;D &lt;b&gt;" if escaped else "R&D <b>"
    assert templating.render(name, None, value="R&D <b>") == expected


def test_deeplink_from_profile(dirs):
    builtin, _ = dirs
    (builtin / "link.md.j2").write_text("[go]({{ deeplink('audit') }})", encoding="utf-8")
    profile = {"telegram": {"bot_username": "@example_bot"}}
    assert templating.render("link.md.j2", profile) == "[go](https://t.me/example_bot?start=audit)"
    assert templating.deeplink("fiscal_tax", SimpleNamespace(telegram=SimpleNamespace(bot_username="b"))) \
        == "https://t.me/b?start=fiscal_tax"


def test_deeplink_without_bot_username_raises():
    with pytest.raises(ValueError):
        templating.deeplink("audit", {"telegram": {"bot_username": None}})


def test_real_profile_default_templates_dir_is_used(tmp_path):
    """A loaded Profile with no explicit templates_dir must still pick up
    profiles/<name>/templates (resolved by Profile.templates_path)."""
    import shutil
    from core.profile import load_profile

    src = Path(__file__).resolve().parent.parent / "profiles" / "example-person.yaml"
    profile_file = tmp_path / "acme.yaml"
    shutil.copy(src, profile_file)
    override = tmp_path / "acme" / "templates"
    override.mkdir(parents=True)
    profile = load_profile(profile_file)
    assert profile.templates_dir is None

    loaders = templating.get_environment(profile).loader.loaders
    assert [l.searchpath for l in loaders][0] == [str(override)]
