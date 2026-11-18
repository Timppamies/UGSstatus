"""Open each Streamlit Community Cloud app in a real browser so it does not go to
sleep, and wake it up if it already has.

Streamlit puts an app to sleep after about 12 hours without visitors; a plain
HTTP request does not count as a visit, so a browser is needed. Run every few
hours by GitHub Actions (.github/workflows/keep-awake.yml).

Usage:  python keep_awake.py [url ...]      (default: the APPS list below)
"""
import sys
import time

from playwright.sync_api import sync_playwright

APPS = [
    "https://ugsstatus.streamlit.app",
    "https://exitflows.streamlit.app",
    "https://entryflows.streamlit.app",
    "https://gasconsumptionfinbaltics.streamlit.app",
    "https://gasconsumptionpersector.streamlit.app",
    "https://kodinkulutus.streamlit.app",
]

WAKE_BUTTON = "button:has-text('get this app back up')"  # on the sleep page
APP_READY = "[data-testid='stApp'], [data-testid='stAppViewContainer']"
LOAD_TIMEOUT_S = 300  # a sleeping app can take several minutes to start
ATTEMPTS = 2  # a slow wake-up gets one more try with a fresh page load


def app_ready(page):
    """True once the Streamlit app itself has rendered (it runs in an iframe
    on *.streamlit.app, or directly in the page)."""
    for frame in page.frames:
        try:
            if frame.query_selector(APP_READY):
                return True
        except Exception:
            pass  # frame navigated away while checking
    return False


def visit(page, url):
    """Returns (ok, note)."""
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    woke = False
    deadline = time.time() + LOAD_TIMEOUT_S
    while time.time() < deadline:
        if app_ready(page):
            # Stay a moment so the visit registers as a session.
            page.wait_for_timeout(10_000)
            return True, "woken up" if woke else "awake"
        button = page.query_selector(WAKE_BUTTON)
        if button and not woke:
            button.click()
            woke = True
        page.wait_for_timeout(3_000)
    return False, "did not load within %d s%s" % (
        LOAD_TIMEOUT_S, " after the wake-up button" if woke else "")


def main():
    urls = sys.argv[1:] or APPS
    failed = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for url in urls:
            for attempt in range(1, ATTEMPTS + 1):
                page = browser.new_page()
                try:
                    ok, note = visit(page, url)
                except Exception as e:
                    ok, note = False, f"error: {type(e).__name__}: {e}"
                page.close()
                if attempt > 1:
                    note += f" (attempt {attempt})"
                if ok or attempt == ATTEMPTS:
                    break
                print(f"RETRY {url}: {note}", flush=True)
            print(f"{'OK  ' if ok else 'FAIL'} {url}: {note}", flush=True)
            if not ok:
                failed.append(url)
        browser.close()
    if failed:
        sys.exit(f"{len(failed)} app(s) did not load: " + ", ".join(failed))


if __name__ == "__main__":
    main()
