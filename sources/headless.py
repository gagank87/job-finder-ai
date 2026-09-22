"""
Headless-browser framework for portals that render jobs with JavaScript and/or
block plain HTTP (Cloudflare, bot filters). This is the shared engine used by
sources/scraped.py.

It:
  * drives a real Chrome via Selenium (already a dependency). Selenium Manager
    downloads the matching chromedriver automatically on first use, so there is
    no manual driver install.
  * degrades HONESTLY. If Selenium or a usable Chrome/driver is missing,
    available() returns (False, reason) and callers skip the source with a
    one-line explanation instead of crashing or fabricating anything.
  * supports a HEADED mode + a persistent browser profile, so a portal that
    needs a human login or a one-time CAPTCHA can be solved ONCE by you and the
    session reused on later runs — the human-in-the-loop path for the harder
    portals (Wellfound, SnagAJob, ...).

No secrets are handled here. Every job link a scraper returns is extracted from
a page a real browser actually loaded (HTTP 200, not a block page); that live
render is the validation for these bot-blocked sites (see sources/scraped.py
and core.run_search for why the requests-based validator is bypassed for them).
Nothing here invents URLs.
"""

import contextlib
import os
import time

import config
import utils

# Substrings that mean "the portal served a challenge / block page", not jobs.
_BLOCK_SIGNALS = (
    "just a moment",
    "verify you are human",
    "are you a human",
    "access denied",
    "captcha",
    "px-captcha",
    "datadome",
    "unusual traffic",
    "enable javascript and cookies to continue",
)


def available():
    """
    (True, "") if a headless browser can be used; (False, reason) otherwise.

    Only checks that Selenium imports — a missing/incompatible Chrome surfaces
    when the driver is actually started (browser() reports it and yields None).
    """
    try:
        import selenium  # noqa: F401
    except Exception:  # noqa: BLE001
        return False, "Selenium is not installed (pip install selenium)."
    return True, ""


def _chrome_options(headless, profile_dir):
    from selenium.webdriver.chrome.options import Options

    o = Options()
    # 'eager' returns from driver.get() at DOMContentLoaded instead of waiting
    # for every sub-resource (ads, trackers, images). We sleep for JS to render
    # afterwards anyway, so this loses nothing — and it's essential for portals
    # like Glassdoor whose background requests never settle, hanging a 'normal'
    # load until the renderer times out.
    o.page_load_strategy = "eager"
    if headless:
        o.add_argument("--headless=new")
    for arg in (
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1400,2000",
        "--disable-blink-features=AutomationControlled",
        "--lang=en-US",
    ):
        o.add_argument(arg)
    o.add_argument("--user-agent=" + config.USER_AGENT)
    if profile_dir:
        # A DEDICATED profile dir (not your real Chrome profile). Lets a manual
        # sign-in / solved CAPTCHA persist across runs for login-walled portals.
        o.add_argument("--user-data-dir=" + os.path.abspath(profile_dir))
    o.add_experimental_option("excludeSwitches", ["enable-automation"])
    o.add_experimental_option("useAutomationExtension", False)
    return o


@contextlib.contextmanager
def browser(headless=True, profile_dir=None):
    """
    Context manager yielding a started Chrome WebDriver, or None if the browser
    can't be started (honest-skip). Always quits the driver on exit.

    Usage:
        with headless.browser() as driver:
            if driver is None:
                return []          # unavailable — skip honestly
            ...
    """
    ok, reason = available()
    driver = None
    if not ok:
        utils.warn(f"Headless browser unavailable: {reason} Skipping scraped portals.")
    else:
        try:
            from selenium import webdriver

            driver = webdriver.Chrome(options=_chrome_options(headless, profile_dir))
            driver.set_page_load_timeout(config.HEADLESS_PAGE_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            # No Chrome installed, driver mismatch, profile locked, etc. — skip.
            utils.warn(f"Could not start headless Chrome ({e}). "
                       f"Skipping scraped portals for this run.")
            driver = None
    try:
        yield driver
    finally:
        if driver is not None:
            with contextlib.suppress(Exception):
                driver.quit()


def load(driver, url, wait=None, wait_for=None):
    """
    Navigate to url and give JS time to render. Returns True on success.
    Never raises — a load failure is narrated and returns False so the caller
    moves on instead of crashing the whole run.

    wait_for: a CSS selector for the content the caller wants. When given, we
    poll (up to HEADLESS_RENDER_WAIT_MAX seconds) until it appears, then return
    as soon as it does — reliable under the 'eager' page-load strategy (where a
    fixed sleep can elapse before a JS-heavy SPA has populated its cards) and
    faster when content arrives early. If it never appears we still return True;
    the caller sees zero cards and skips that portal honestly.
    """
    try:
        driver.get(url)
    except Exception as e:  # noqa: BLE001
        utils.warn(f"Headless load failed for {url}: {e}")
        return False
    if wait_for:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait

            WebDriverWait(driver, config.HEADLESS_RENDER_WAIT_MAX).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_for)))
            time.sleep(1)  # small settle so the rest of the cards render too
        except Exception:  # noqa: BLE001 — timeout/none: let the caller decide
            pass
        return True
    time.sleep(wait if wait is not None else config.HEADLESS_RENDER_WAIT)
    return True


def soup(driver):
    """BeautifulSoup of the browser's CURRENT, JS-rendered DOM."""
    from bs4 import BeautifulSoup

    return BeautifulSoup(driver.page_source, "html.parser")


def looks_blocked(driver):
    """
    True if the current page looks like a CAPTCHA / bot-challenge / access-denied
    page rather than real content. Lets a scraper stop honestly instead of
    parsing a block page as if it were jobs.
    """
    low = (driver.page_source or "").lower()
    return any(sig in low for sig in _BLOCK_SIGNALS)
