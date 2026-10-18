"""
Single reader/writer of the project .env file (stdlib only).

Reading keeps the historical parser semantics: `KEY=value` per line, `#` comments,
surrounding quotes stripped, no inline comments, the last occurrence of a key wins.

Writing updates keys in place and keeps everything else untouched: unknown keys,
comments, blank lines and order. The file is rewritten in place (same inode),
not via tmp + os.replace: in docker-compose ./.env is bind-mounted into the
container as a single file, and a rename would detach it from the host copy.
The previous content is saved to `<name>.bak-<YYYYmmdd-HHMMSS>` before every write
(the newest BACKUPS_KEPT copies are kept).
"""
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Mapping, Optional, Union

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
BACKUPS_KEPT = 5


def _parse_line(line: str) -> Optional[tuple]:
    """Returns (key, value) for an assignment line, None for comments/blank/garbage."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    k, v = line.split("=", 1)
    return k.strip(), v.strip().strip("'\"")


def read(path: Union[str, Path]) -> Dict[str, str]:
    """Parses the .env file into a dict; a missing file is an empty dict."""
    path = Path(path)
    res: Dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            parsed = _parse_line(line)
            if parsed:
                res[parsed[0]] = parsed[1]
    return res


def _validate(updates: Mapping[str, object]) -> Dict[str, str]:
    clean: Dict[str, str] = {}
    for k, v in updates.items():
        if v is None:
            continue
        if not isinstance(k, str) or not KEY_RE.match(k):
            raise ValueError(f"invalid .env key: {k!r}")
        v = str(v)
        if "\n" in v or "\r" in v:
            raise ValueError(f"value for {k} contains a line break")
        clean[k] = v
    return clean


def _backup(path: Path) -> Path:
    """Copies the file to <name>.bak-YYYYmmdd-HHMMSS and keeps the newest BACKUPS_KEPT copies."""
    dst = path.with_name(f"{path.name}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, dst)
    old = sorted(path.parent.glob(f"{path.name}.bak-*"))[:-BACKUPS_KEPT]
    for p in old:
        p.unlink()
    return dst


def write(path: Union[str, Path], updates: Mapping[str, object], backup: bool = True) -> Dict[str, str]:
    """
    Sets `updates` in the .env file and returns the resulting parsed content.
    None values are skipped; every occurrence of an updated key is rewritten,
    new keys are appended at the end. Raises ValueError on a bad key or a value
    with a line break (it would inject extra keys).
    """
    path = Path(path)
    clean = _validate(updates)

    # bytes, not read_text: keep original line endings (universal newlines would drop \r)
    old_text = path.read_bytes().decode("utf-8") if path.exists() else ""
    if backup and path.exists():
        _backup(path)

    lines = old_text.splitlines(keepends=True)
    seen = set()
    for i, line in enumerate(lines):
        parsed = _parse_line(line)
        if parsed and parsed[0] in clean:
            key = parsed[0]
            ending = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
            lines[i] = f"{key}={clean[key]}{ending}"
            seen.add(key)

    missing = [k for k in clean if k not in seen]
    if missing:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += [f"{k}={clean[k]}\n" for k in missing]

    new_text = "".join(lines)
    if new_text != old_text or not path.exists():
        with open(path, "wb") as f:
            f.write(new_text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
    return read(path)
