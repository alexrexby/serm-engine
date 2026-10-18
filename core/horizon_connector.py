"""
Compatibility shim: the radar moved to core/radar.py (class NewsRadar).
Old imports keep working; module constants are now computed from the profile on access.
"""
from core.radar import NewsRadar, CACHE_FILE as HORIZON_CACHE_FILE  # noqa: F401

HorizonConnector = NewsRadar


def __getattr__(name):
    from core import radar
    from core.profile import get_profile
    if name == "EVERGREEN_VIBECODING_CASES":
        return radar.evergreen_cases()
    if name == "HORIZON_FEEDS":
        return radar.feeds()
    if name == "RELEVANT_KEYWORDS":
        return list(get_profile().radar.relevant_keywords)
    if name == "NOISE_KEYWORDS":
        return list(get_profile().radar.noise_keywords)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
