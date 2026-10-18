"""
Jinja2 rendering over the brand profile.

Lookup order: profile override templates (`profile.templates_dir`) first, then the neutral
built-in `templates/` of the project. A profile template with the same relative name wins.
Autoescape is on only for web HTML (`admin/**`, `lead_magnet/*.html.j2`). Telegram HTML
(`bot/**`, `telegram/**`) and markdown/text are rendered verbatim: untrusted input there
(TG user names, RSS text, LLM output, error text) must be escaped explicitly with `|e`.
Undefined variables raise (StrictUndefined): a typo in a profile field fails the render,
it never prints an empty string.

`profile` is accepted as an object or a dict (only `templates_dir` and
`telegram.bot_username` are read here).
"""
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, StrictUndefined

BUILTIN_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
# (path prefix, name suffix) pairs with autoescape on
AUTOESCAPE_RULES = (("admin/", ""), ("lead_magnet/", ".html.j2"))


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _autoescape(template_name: Optional[str]) -> bool:
    if not template_name:
        return False
    return any(template_name.startswith(prefix) and template_name.endswith(suffix)
               for prefix, suffix in AUTOESCAPE_RULES)


@lru_cache(maxsize=None)
def _environment(templates_dir: Optional[str], builtin_dir: str) -> Environment:
    loaders = []
    if templates_dir:
        loaders.append(FileSystemLoader(templates_dir))
    loaders.append(FileSystemLoader(builtin_dir))
    return Environment(
        loader=ChoiceLoader(loaders),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=_autoescape,
    )


def get_environment(profile: Optional[Any] = None) -> Environment:
    # Profile objects resolve the default profiles/<name>/templates via templates_path;
    # plain dicts only carry templates_dir.
    templates_dir = _get(profile, "templates_path") or _get(profile, "templates_dir")
    return _environment(str(templates_dir) if templates_dir else None, str(BUILTIN_TEMPLATES_DIR))


def deeplink(slug: str, profile: Optional[Any] = None) -> str:
    """Telegram bot deep link: https://t.me/<bot_username>?start=<slug>."""
    bot_username = _get(_get(profile, "telegram"), "bot_username")
    if not bot_username:
        raise ValueError("deeplink: profile.telegram.bot_username is not set")
    return f"https://t.me/{bot_username.lstrip('@')}?start={slug}"


def render(name: str, profile: Optional[Any] = None, **ctx: Any) -> str:
    """Renders template `name` with `profile` and `deeplink(slug)` in context; `ctx` overrides."""
    context = {"profile": profile, "deeplink": lambda slug: deeplink(slug, profile)}
    context.update(ctx)
    return get_environment(profile).get_template(name).render(**context)
