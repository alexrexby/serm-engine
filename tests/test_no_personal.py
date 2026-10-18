"""
Neutrality of what goes to the public repository: no owner brand markers in the files git will ship.

The markers are NOT literals here (a public test must not publish the strings it guards against):
they come from the non-example profiles (name, display_name, aliases, telegram.*, campaign_id,
deploy.server_url) plus `.local/markers.txt` — both outside git. Without them (public checkout)
the tests skip.

(б) every file of `git ls-files` (plus untracked, not ignored — they land in the next commit),
    except the owner's local regression and deploy: tests/golden/**, tests/test_golden.py
    (byte-for-byte regression of the owner's installation — private, not part of the public upload),
    scripts/deploy_to_server.sh, .claude/**;
(в) string literals of the Python code (AST: implicit concatenation is joined, f-string parts
    included). Soft by design: only brand markers; Cyrillic log/CLI messages are allowed.
The only allowed occurrence is ALLOWED: the source code link required by AGPL-3.0 §13.
Other tests check their rendered texts with private_markers() + find_markers().
"""
import ast
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.profile import PROFILES_DIR  # noqa: E402

EXCLUDED_PREFIXES = ("tests/golden/", ".claude/")
EXCLUDED_FILES = ("tests/test_golden.py", "scripts/deploy_to_server.sh")
# Source code link in the admin footer and README (AGPL-3.0 §13); any other occurrence is an error
ALLOWED = ("github.com/alexrexby/serm-engine",)
CODE_PATHS = ("core", "publishers", "config", "cli.py", "scheduler.py")


def private_markers(root: Path = PROJECT_ROOT, profiles_dir: Path = PROFILES_DIR) -> list:
    markers = set()
    for path in profiles_dir.glob("*.yaml"):
        if path.stem.startswith("example"):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        tg = data.get("telegram") or {}
        values = [data.get("name"), data.get("display_name"), data.get("campaign_id"),
                  (data.get("deploy") or {}).get("server_url"), *(data.get("aliases") or []),
                  tg.get("channel"), tg.get("contact"), tg.get("bot_username")]
        markers.update(str(v).lstrip("@") for v in values if v)
    local = root / ".local" / "markers.txt"
    if local.is_file():
        markers.update(line.strip() for line in local.read_text(encoding="utf-8").splitlines() if line.strip())
    return sorted(m for m in markers if len(m) >= 4)


def public_files(root: Path) -> list:
    """Files the public repo would get: tracked + untracked-not-ignored, minus the owner's local ones."""
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         capture_output=True, check=True).stdout.decode("utf-8")
    names = sorted({n for n in out.split("\0") if n})
    return [n for n in names
            if not n.startswith(EXCLUDED_PREFIXES) and n not in EXCLUDED_FILES and (root / n).is_file()]


def find_markers(text: str, markers: list) -> list:
    low = text.lower()
    for allowed in ALLOWED:
        low = low.replace(allowed.lower(), " ")
    return [m for m in markers if m.lower() in low]


def scan_files(root: Path, names: list, markers: list) -> list:
    hits = []
    for name in names:
        text = (root / name).read_bytes().decode("utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            for marker in find_markers(line, markers):
                hits.append(f"{name}:{lineno}: {marker}")
    return hits


def scan_code_literals(root: Path, markers: list) -> list:
    files = []
    for entry in CODE_PATHS:
        path = root / entry
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path] if path.is_file() else [])
    hits = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for marker in find_markers(node.value, markers):
                    hits.append(f"{path.relative_to(root)}:{node.lineno}: {marker}")
    return hits


@pytest.fixture(scope="module")
def markers():
    found = private_markers()
    if not found:
        pytest.skip("no private profile / .local/markers.txt (public checkout)")
    return found


def test_no_markers_in_public_files(markers):
    try:
        names = public_files(PROJECT_ROOT)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    assert names, "positive control: git ls-files returned nothing"
    hits = scan_files(PROJECT_ROOT, names, markers)
    assert not hits, f"{len(hits)} personal markers in public files:\n" + "\n".join(hits[:200])


def test_no_markers_in_code_literals(markers):
    hits = scan_code_literals(PROJECT_ROOT, markers)
    assert not hits, "personal markers in string literals:\n" + "\n".join(hits)


def test_scanner_sees_markers(tmp_path, markers):
    """Positive control: the scanners do find a marker (lower-cased, split by implicit concatenation)."""
    marker = markers[0]
    half = len(marker) // 2
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "m.py").write_text(f"X = ({marker[:half]!r} {marker[half:].upper()!r})\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(f"text {marker.lower()} text\n", encoding="utf-8")
    assert scan_code_literals(tmp_path, [marker]) == [f"core/m.py:1: {marker}"]
    assert scan_files(tmp_path, ["README.md"], [marker]) == [f"README.md:1: {marker}"]


def test_only_the_source_link_is_allowed():
    """ALLOWED is an exact string: the same account in any other context is still a marker."""
    link = ALLOWED[0]
    account = link.split("/")[1]
    assert find_markers(f"git clone https://{link}\n", [account]) == []
    assert find_markers(f"https://{link.upper()}", [account]) == []
    assert find_markers(f"https://github.com/{account}/other", [account]) == [account]
    assert find_markers(f"https://{link} and t.me/{account}", [account]) == [account]
