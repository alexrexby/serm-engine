"""
Admin texts moved out of the code (stage 4): editorial pitches (pitch/<platform>.txt.j2) and the
analytics sample status (analytics/sample_status.yaml). On the own installation the output must be
byte-for-byte the old f-strings; on example profiles it renders from the neutral templates.
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

ADMIN_TOKEN = "test-admin-token-0123456789"
os.environ.setdefault("ADMIN_TOKEN", ADMIN_TOKEN)

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import core.admin as admin  # noqa: E402
from core.db import Database  # noqa: E402
from core.models import Article  # noqa: E402
from core.profile import PROFILES_DIR, ProfileError, get_profile, resolve_profile_path  # noqa: E402

TOKEN = os.environ["ADMIN_TOKEN"]
TITLE, LEAD = "Кейс <A&B>", "Лид «тест»"

# Output of core/admin.py and core/db.py before the texts moved to templates (commit 3975d65)
OLD_PITCH = {
    "tbank": (f"📨 ПИТЧ ДЛЯ РЕДАКЦИИ Т-БАНК «СЕКРЕТЫ БИЗНЕСА»:\nТема: {TITLE}\nСуть кейса: {LEAD}\n"
              f"Целевая аудитория: Предприниматели, малый и средний бизнес\n"
              f"Канал отправки: Telegram-бот @bs_community_bot"),
    "biz360": (f"📨 ПИТЧ ДЛЯ РЕДАКЦИИ BIZ360.RU:\nТема: {TITLE}\nЛид: {LEAD}\n"
               f"Формат: Практический кейс / Опыт автоматизации бизнеса\n"
               f"Канал подачи: https://biz360.ru/about/feedback/"),
    "rb": (f"📨 ПИТЧ ДЛЯ РЕДАКЦИИ RB.RU (КОЛОНКА ЭКСПЕРТА):\nТема: {TITLE}\nЛид: {LEAD}\n"
           f"Формат: Мнение / Колонка предпринимателя в соло\nКанал подачи: https://rb.ru/opinion/"),
}


def _old_sample(starts, sample_min=50, sample_target=100):
    if starts < sample_min:
        return (f"Калибровка ({starts}/{sample_min} мин.)",
                f"Накопление базовой выборки. До минимального порога осталось {sample_min - starts} переходов. "
                f"Принимать решения по контент-плану пока рано.")
    if starts < sample_target:
        return (f"Первичный тренд ({starts}/{sample_target})",
                f"Минимальный порог в 50 переходов пройден. Проявляются первые лидеры. "
                f"Добираем ещё {sample_target - starts} переходов до полной достоверности.")
    return (f"Выборка подтверждена ({starts}/{sample_target}+)",
            "Статистическая мощность выборки достаточна. Разница между темами математически достоверна — "
            "масштабируйте тему-победителя.")


def _profile_path(name):
    if name != "own":
        return PROFILES_DIR / f"{name}.yaml"
    try:
        return resolve_profile_path()
    except ProfileError:
        pytest.skip("no own profile (public checkout)")


@pytest.fixture(params=["example-person", "example-company", "own"])
def installation(request, tmp_path, monkeypatch):
    monkeypatch.delenv("SERM_PROFILE", raising=False)
    path = _profile_path(request.param)
    monkeypatch.setenv("SERM_PROFILE", str(path))
    monkeypatch.setattr(admin, "ENV_PATH", tmp_path / "dotenv")
    get_profile.cache_clear()
    yield request.param
    get_profile.cache_clear()


def _pitches():
    db = Database()
    for platform in OLD_PITCH:
        db.add_article(Article(id=f"pitch-{platform}", campaign_id="pitch01", platform=platform, title=TITLE,
                               lead=LEAD, content_markdown="body", content_html="<p>body</p>", tags="",
                               status="ready"))

    async def main():
        async with TestClient(TestServer(admin.create_admin_app(), host="127.0.0.1")) as client:
            result = {}
            for platform in [*OLD_PITCH, "vc"]:
                if platform == "vc":
                    db.add_article(Article(id="pitch-vc", campaign_id="pitch01", platform="vc", title=TITLE,
                                           lead=LEAD, content_markdown="body", content_html="", tags="",
                                           status="ready"))
                resp = await client.get(f"/api/articles/content?id=pitch-{platform}",
                                        headers={"X-Admin-Token": TOKEN})
                assert resp.status == 200
                result[platform] = (await resp.json())["pitch_text"]
            return result
    return asyncio.run(main())


def test_editorial_pitch_from_templates(installation):
    pitches = _pitches()
    assert pitches["vc"] is None, "non-editorial platforms get no pitch"
    for platform, old in OLD_PITCH.items():
        text = pitches[platform]
        if installation == "own":
            assert text == old, platform
        else:
            # Article fields are inserted raw (plain text, shown via innerText), never HTML-escaped
            assert f"Тема: {TITLE}" in text and LEAD in text, platform


@pytest.mark.parametrize("starts", [0, 1, 49, 50, 99, 100, 150])
def test_sample_status_from_templates(installation, tmp_path, starts):
    db = Database(tmp_path / "t.db")
    for uid in range(starts):
        db.log_bot_event(user_id=uid, username="u", first_name="f", event_type="start", slug="audit", details="")
    summary = db.get_funnel_summary()
    status, description = _old_sample(starts)
    if installation == "own":
        assert (summary["sample_status"], summary["sample_description"]) == (status, description)
    else:
        assert str(starts) in summary["sample_status"] and summary["sample_description"]
    assert summary["sample_stage"] == ("calibrating" if starts < 50 else "emerging" if starts < 100 else "significant")
