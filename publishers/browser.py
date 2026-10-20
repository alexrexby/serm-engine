"""
Headless browser for publishers: the saved session (storage_state.json from the admin or `serm login`)
goes to Browser.new_context — launch_persistent_context has no storage_state parameter.
"""
from pathlib import Path

LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]


def open_context(p, session_dir: Path):
    """Returns (browser, context); the caller closes the browser."""
    browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
    storage_file = Path(session_dir) / "storage_state.json"
    try:
        if storage_file.exists() and storage_file.stat().st_size > 50:
            context = browser.new_context(storage_state=str(storage_file))
        else:
            context = browser.new_context()
    except Exception:
        # e.g. a storage_state.json cut by a partial write; the caller's finally is not entered yet
        browser.close()
        raise
    return browser, context
