"""Article order: platforms rotate (a test period covers all of them), a platform's repeated title waits."""
from core.autopilot import PLATFORM_PRIORITY, pick_next_article
from core.models import Article

PLATFORMS = sorted(PLATFORM_PRIORITY, key=PLATFORM_PRIORITY.get)


def _a(i, platform, title):
    return Article(id=f"{platform}{i}", campaign_id="c", platform=platform, title=title, lead="",
                   content_markdown="b", content_html="", tags="", status="ready")


def _prod_like_queue():
    # Like the live campaign: per platform three variants with one title, then two distinct titles
    queue = []
    for p in PLATFORMS:
        queue += [_a(1, p, f"{p} A"), _a(2, p, f"{p} A"), _a(3, p, f"{p} A"), _a(4, p, f"{p} B"), _a(5, p, f"{p} C")]
    return queue


def _publish_all(queue, n):
    ready, history, picked = list(queue), [], []
    for day in range(n):
        a = pick_next_article(ready, history)
        ready.remove(a)
        picked.append(a)
        history.append({"platform": a.platform, "title": a.title, "timestamp": f"2026-10-{19 + day:02d}T10:30"})
    return picked


DIRECT = ["dzen", "vc", "spark", "executive"]  # publish to readers; rb/tbank/biz360 only prepare a pitch file


def test_rounds_cover_every_publishing_platform_editorial_last():
    queue = _prod_like_queue()
    picked = _publish_all(queue, len(queue))
    platforms = [a.platform for a in picked]
    assert platforms[:8] == DIRECT * 2
    assert set(platforms[:20]) == set(DIRECT)  # all 20 direct articles before any editorial pitch
    assert platforms[20:23] == ["rb", "tbank", "biz360"]


def test_repeated_title_waits_for_the_platforms_fresh_titles():
    picked = _publish_all(_prod_like_queue(), len(PLATFORMS) * 3)
    dzen = [a.title for a in picked if a.platform == "dzen"]
    assert dzen == ["dzen A", "dzen B", "dzen C", "dzen A", "dzen A"]  # other "A" variants only after B and C


def test_platform_tried_last_goes_to_the_end_of_the_round():
    queue = [_a(1, "dzen", "D"), _a(1, "vc", "V")]
    # dzen failed this morning (no history entry), vc never tried
    assert pick_next_article(queue, [], {"dzen": "2026-10-19T10:30"}).platform == "vc"
    # vc tried later than dzen: back to dzen
    assert pick_next_article(queue, [], {"dzen": "2026-10-19T10:30", "vc": "2026-10-20T10:30"}).platform == "dzen"


def test_single_platform_keeps_queue_order():
    queue = [_a(1, "vc", "T1"), _a(2, "vc", "T2")]
    assert pick_next_article(queue, [], {"vc": "2026-10-19T10:30"}).id == "vc1"
