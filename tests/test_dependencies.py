"""
requirements.txt and [project.dependencies] of pyproject.toml list the same packages (by name):
both are kept (owner's decision 2026-10-18), so a package added to one of them only is an error.
"""
import re
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def package_name(spec: str) -> str:
    """PEP 503 normalized name of a requirement line ('' for comments, blanks and pip options)."""
    spec = spec.split("#", 1)[0]
    match = NAME.match(spec)
    return re.sub(r"[-_.]+", "-", match.group(1)).lower() if match else ""


def requirements_names(text: str) -> set:
    return {name for name in map(package_name, text.splitlines()) if name}


def pyproject_names(text: str) -> set:
    return {package_name(spec) for spec in tomllib.loads(text)["project"]["dependencies"]}


def test_requirements_match_pyproject():
    req = requirements_names((PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8"))
    pyp = pyproject_names((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert req, "positive control: requirements.txt parsed to nothing"
    assert req == pyp, f"only in requirements.txt: {sorted(req - pyp)}; only in pyproject.toml: {sorted(pyp - req)}"


def test_pydantic_settings_is_pinned_to_the_checked_minor():
    # CFG-M1: older 2.x key the process env source by the alias found, not the canonical name, so
    # "process env > file" breaks for aliases (checked on 2.2.1); 2.16 is the version under test
    req = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    pyp = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    specs = [s.replace(" ", "") for s in req + pyp if package_name(s) == "pydantic-settings"]
    assert specs == ["pydantic-settings~=2.16"] * 2


def test_names_are_normalized():
    assert requirements_names("# c\n\nPyYAML>=6\npydantic_settings >= 2  # x\n-r other.txt\naiogram[fast]==3.4\n") == \
        {"pyyaml", "pydantic-settings", "aiogram"}
