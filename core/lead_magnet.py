"""
Dynamic Lead Magnet Generator: an audit guide / checklist for a preset or ANY topic,
in Markdown and print-ready PDF, over the brand profile.

Presets come from the profile: each `lead_magnets[].template` (e.g. `lead_magnet/checklist`) names
a data file `<template>.yaml`; its basename is the preset key and names the output files
lead_magnet_<key>.md/.pdf. A topic that is not a preset key gets `lead_magnet/custom.yaml`.
Data files are YAML whose string values are Jinja templates rendered AFTER parsing, so a topic typed
in the admin panel or taken from RSS cannot break the YAML. Layout: lead_magnet/doc.md.j2 and
lead_magnet/doc.html.j2 (web HTML, autoescaped).
"""
import asyncio
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import yaml

from core.templating import get_environment, render

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

CUSTOM_PRESET = "lead_magnet/custom.yaml"


def _installation_profile():
    from core.profile import get_profile
    return get_profile()


def render_data_template(name: str, profile: Any, **ctx: Any) -> Any:
    """Loads YAML template `name` (profile override first) and renders every string value with ctx."""
    env = get_environment(profile)
    source, _, _ = env.loader.get_source(env, name)
    data = yaml.safe_load(source)

    def fill(value):
        if isinstance(value, str):
            return env.from_string(value).render(profile=profile, **ctx)
        if isinstance(value, list):
            return [fill(v) for v in value]
        if isinstance(value, dict):
            return {k: fill(v) for k, v in value.items()}
        return value

    return fill(data)


def default_magnet(profile: Any):
    """profile.default_lead_magnet, else the first lead magnet, else None."""
    return profile.lead_magnet() or (profile.lead_magnets[0] if profile.lead_magnets else None)


def preset_key(magnet: Any) -> str:
    return magnet.template.rsplit("/", 1)[-1]


def load_presets(profile: Any) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[str]]]:
    """(preset key → preset data, preset key → topic keywords), in the profile's lead_magnets order."""
    presets: Dict[str, Dict[str, Any]] = {}
    keywords: Dict[str, List[str]] = {}
    for magnet in profile.lead_magnets:
        key = preset_key(magnet)
        if key in presets:
            continue
        data = render_data_template(f"{magnet.template}.yaml", profile, magnet=magnet, topic="")
        keywords[key] = [w.lower() for w in data.pop("keywords", None) or []]
        presets[key] = data
    return presets, keywords


class _ProfilePresets(Mapping):
    """LEAD_MAGNET_TOPIC_PRESETS: read from the installation profile on every access (not at import)."""

    def _presets(self) -> Dict[str, Dict[str, Any]]:
        return load_presets(_installation_profile())[0]

    def __getitem__(self, key: str) -> Dict[str, Any]:
        return self._presets()[key]

    def __iter__(self):
        return iter(self._presets())

    def __len__(self) -> int:
        return len(self._presets())


LEAD_MAGNET_TOPIC_PRESETS = _ProfilePresets()


class DynamicLeadMagnetGenerator:
    """Generates customized, business-oriented Lead Magnets in Markdown & PDF."""

    def __init__(self, output_dir: Path, profile: Optional[Any] = None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._profile = profile

    @property
    def profile(self):
        if self._profile is None:
            self._profile = _installation_profile()
        return self._profile

    def _default_key(self) -> str:
        magnet = default_magnet(self.profile)
        if magnet is None:
            raise ValueError("profile has no lead_magnets: nothing to generate")
        return preset_key(magnet)

    def detect_preset_key(self, topic: str) -> str:
        topic_lower = topic.lower()
        _, keywords = load_presets(self.profile)
        for key, words in keywords.items():
            if any(w in topic_lower for w in words):
                return key
        return self._default_key()

    def _doc_context(self, preset: Dict[str, Any], brand_name: str, channel: str) -> Dict[str, Any]:
        return {"preset": preset, "brand_name": brand_name, "channel": channel,
                "channel_url": f"https://t.me/{channel.lstrip('@')}"}

    def render_markdown(self, preset: Dict[str, Any], brand_name: str, channel: str) -> str:
        return render("lead_magnet/doc.md.j2", self.profile, **self._doc_context(preset, brand_name, channel))

    def render_html(self, preset: Dict[str, Any], brand_name: str, channel: str) -> str:
        return render("lead_magnet/doc.html.j2", self.profile, **self._doc_context(preset, brand_name, channel))

    async def generate_for_topic(self, topic: str, brand_name: Optional[str] = None, channel: Optional[str] = None) -> Dict[str, Any]:
        profile = self.profile
        brand_name = brand_name if brand_name is not None else profile.display_name
        channel = channel if channel is not None else profile.telegram.contact
        presets, _ = load_presets(profile)
        if topic in presets:
            preset_key = topic
            preset = presets[preset_key]
        else:
            preset_key = self.detect_preset_key(topic)
            if topic.strip() and len(topic.strip()) > 5:
                # Custom topic - build bespoke whitepaper structure
                preset = render_data_template(CUSTOM_PRESET, profile, magnet=None, topic=topic.strip())
            else:
                preset = presets[preset_key]

        slug = re.sub(r'[^a-zA-Z0-9_]', '_', preset_key.lower())[:30]
        md_path = self.output_dir / f"lead_magnet_{slug}.md"
        pdf_path = self.output_dir / f"lead_magnet_{slug}.pdf"

        # 1. Write Markdown
        md_content = self.render_markdown(preset, brand_name, channel)
        md_path.write_text(md_content, encoding="utf-8")

        # 2. Render PDF via Chromium
        html_content = self.render_html(preset, brand_name, channel)
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            page = await browser.new_page()
            await page.set_content(html_content, wait_until="networkidle")
            await page.pdf(
                path=str(pdf_path),
                format="A4",
                print_background=True,
                margin={"top": "8mm", "bottom": "8mm", "left": "8mm", "right": "8mm"}
            )
            await browser.close()

        # Update active default lead magnet: the file the bot sends for the default deep link
        magnet = default_magnet(profile)
        if magnet is not None:
            default_pdf = self.output_dir / magnet.file_prefix
            default_md = default_pdf.with_suffix(".md")
            default_pdf.write_bytes(pdf_path.read_bytes())
            default_md.write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")

        return {
            "markdown": md_path,
            "pdf": pdf_path,
            "preset_key": preset_key,
            "title": preset["title"],
            "slug": slug,
            "size_bytes": pdf_path.stat().st_size
        }


# Backward compatibility wrapper
class LeadMagnetGenerator:
    def __init__(self, output_dir: Path, profile: Optional[Any] = None):
        self.dynamic = DynamicLeadMagnetGenerator(output_dir, profile)

    def generate_all(self, topic: Optional[str] = None) -> Dict[str, Path]:
        res = asyncio.run(self.dynamic.generate_for_topic(topic or self.dynamic._default_key()))
        return {
            "markdown": res["markdown"],
            "pdf": res["pdf"]
        }

    def generate_markdown(self, filename: Optional[str] = None) -> Path:
        res = self.generate_all()
        return res["markdown"]

    async def generate_pdf(self, filename: Optional[str] = None) -> Path:
        res = await self.dynamic.generate_for_topic(self.dynamic._default_key())
        return res["pdf"]
