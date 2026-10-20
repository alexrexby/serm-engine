"""Links survive the markdown → HTML the publishers paste into the editors.

Prod 2026-10-20: every t.me link in the published post was broken —
`_` inside the URL was taken for italics (`t.me/name<em>bot?start=dzen</em>audit`), so a reader could
not reach the bot from the article.
"""
import re

import pytest

from core.markdown_parser import markdown_to_html, parse_inline_formatting

BOT = "https://t.me/example_bot?start=dzen_audit"


def hrefs(html):
    return re.findall(r'href="([^"]*)"', html)


@pytest.mark.parametrize("platform", ["dzen", "vc", "spark", "executive", "general"])
def test_markdown_link_url_with_underscores_is_intact(platform):
    html = markdown_to_html(f"Запишитесь [в боте]({BOT}) или напишите [@some_user](https://t.me/some_user).",
                            platform=platform)

    assert hrefs(html) == [BOT, "https://t.me/some_user"]
    assert "<em>" not in html
    assert "@some_user</a>" in html


def test_bare_url_with_underscores_is_intact():
    html = parse_inline_formatting(f"Бот: {BOT} — пишите")

    assert BOT in html and "<em>" not in html


def test_intraword_underscores_are_not_italics():
    assert parse_inline_formatting("переменная snake_case_name тут") == "переменная snake_case_name тут"


def test_emphasis_still_works_around_links():
    html = parse_inline_formatting(f"_важно_: **[бот]({BOT})** и *курсив*")

    assert html.startswith("<em>важно</em>:")
    assert f'<strong><a href="{BOT}"' in html
    assert "<em>курсив</em>" in html
    assert hrefs(html) == [BOT]


def test_emphasis_inside_link_text():
    html = parse_inline_formatting(f"[**бесплатная** диагностика]({BOT})")

    assert html == f'<a href="{BOT}" target="_blank" rel="noopener"><strong>бесплатная</strong> диагностика</a>'
