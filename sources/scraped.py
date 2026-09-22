"""
Headless-scraped job portals.

Some portals render results with JavaScript and/or block plain HTTP, so the
requests-based sources can't read them. This module drives a real Chrome (via
the shared framework in sources/headless.py) to read those portals the way a
person's browser would, then maps each posting onto the standard job dict used
everywhere else in the tool.

Currently implemented (each enabled per-portal in config.SCRAPERS):
  * SimplyHired — a major aggregator; JS-rendered SERP that 403s plain HTTP.
  * Naukri — India's largest board; server-rendered cards, no login/CAPTCHA.
  * Wellfound — startup/tech jobs; the /role/<slug> pages render headless
    WITHOUT a login (the logged-in /jobs feed hangs the renderer, so we avoid
    it). Company is read from the nearest ancestor of each job link.
  * Glassdoor — hostile to headless: a 'normal' page load hangs the renderer,
    so the shared browser uses pageLoadStrategy='eager' (headless.py). It also
    embeds 'captcha' in page JSON on GOOD pages, so looks_blocked() would
    false-positive; instead card presence is the ground truth. JSearch already
    aggregates Glassdoor, so this is a redundant-but-direct fallback.

The dispatcher is structured so more portals slot in as adapters in _ADAPTERS.
A portal that truly needs a human login/CAPTCHA can run via HEADLESS_MODE='headed'
+ a one-time manual sign-in stored in the persistent browser profile.

Honesty guarantees (identical to every other source):
  * Skips honestly (returns []) if the browser is unavailable or a portal
    serves a block/CAPTCHA page — never fabricates jobs, never crashes the run.
  * Every job carries a REAL link extracted from a page the browser actually
    loaded (HTTP 200, not blocked). Because these sites 403 a bare `requests`
    call, the core pipeline's requests-based link validator would give FALSE
    negatives and dishonestly drop live postings — so scraped jobs are marked
    `_validated=True` (the live SERP render IS their validation) and core skips
    re-validating them. See core.run_search step 7.
"""

import re
import time
import urllib.parse

import config
import cvprofile
import salary as salarymod
import utils
from sources import headless

_SH_BASE = "https://www.simplyhired.com"
_NAUKRI_BASE = "https://www.naukri.com"
_WF_BASE = "https://wellfound.com"
_GD_BASE = "https://www.glassdoor.com"


def _slug(role):
    """A URL slug from a role keyword: lowercase, non-alnum runs -> single dash."""
    return re.sub(r"[^a-z0-9]+", "-", (role or "").lower()).strip("-")


def _salary_if_numeric(raw):
    """Normalize a salary chip only if it actually contains a number; otherwise
    return (None, "") so 'Not disclosed'/'Competitive' is treated as unknown
    (kept by the salary rule) rather than mis-parsed."""
    if raw and any(ch.isdigit() for ch in raw):
        return salarymod.normalize(raw=raw)
    return None, ""


# ---------------------------------------------------------------------------
# SimplyHired
# ---------------------------------------------------------------------------
def _sh_location(country):
    """Map a chosen country label to SimplyHired's `l` (location) param."""
    c = (country or "").lower()
    if c.startswith("india"):
        return "India"
    if "united states" in c or c in ("us", "usa"):
        return "United States"
    return ""  # Remote / Worldwide — no location filter


def _txt(card, selector):
    el = card.select_one(selector)
    return el.get_text(" ", strip=True) if el else ""


def _sh_card(card, role):
    """Map one SimplyHired result card to a job dict, or None if unusable."""
    a = (card.select_one("[data-testid=searchSerpJobTitle] a")
         or card.select_one("a[href^='/job/']"))
    href = a.get("href") if a else ""
    if not href:
        return None
    link = href if href.startswith("http") else _SH_BASE + href

    title = _txt(card, "[data-testid=searchSerpJobTitle]") or (
        a.get_text(" ", strip=True) if a else "")
    if not title:
        return None

    company = _txt(card, "[data-testid=companyName]")
    location = _txt(card, "[data-testid=searchSerpJobLocation]")
    sal_txt = _txt(card, "[data-testid=salaryChip-0]")
    lpa, disp = salarymod.normalize(raw=sal_txt) if sal_txt else (None, "")

    # Stable job id = the /job/<token> slug from the canonical apply link.
    job_id = href.rstrip("/").split("/job/")[-1] if "/job/" in href else href

    return {
        "source": "SimplyHired",
        "company": company,
        "title": title,
        "job_id": job_id,
        "job_link": link,
        "location": location,
        "salary": disp,
        "salary_lpa": lpa,
        "remote": "remote" in location.lower(),
        "match_score": cvprofile.match_score(title, [role]),
        # Full JD lives behind JS on the job page; left empty so the pipeline's
        # analyzer marks it "Unknown" (kept) rather than judging a partial blurb.
        "job_description": "",
        # This link came off a live, non-blocked SERP — that render is its
        # validation (the site 403s plain requests). See module docstring.
        "_validated": True,
    }


def _simplyhired(driver, roles, country):
    """Scrape SimplyHired for each role; returns raw (unvalidated-by-requests)
    job dicts. Stops a role honestly if a block page appears."""
    loc = _sh_location(country)
    out = []
    for role in roles:
        for page in range(1, config.HEADLESS_MAX_PAGES + 1):
            params = {"q": role, "l": loc}
            if page > 1:
                params["pn"] = str(page)
            url = _SH_BASE + "/search?" + urllib.parse.urlencode(params)
            if not headless.load(driver, url,
                                 wait_for="[data-testid=searchSerpJob]"):
                break
            if headless.looks_blocked(driver):
                utils.warn(f"SimplyHired looks blocked (CAPTCHA/challenge) for "
                           f"'{role}' - stopping it for this run.")
                break
            cards = headless.soup(driver).select("[data-testid=searchSerpJob]")
            if not cards:
                break  # no more results for this role
            got = [j for j in (_sh_card(c, role) for c in cards) if j]
            out.extend(got)
            utils.info(f"SimplyHired '{role}' page {page}: {len(got)} job(s).")
            utils.polite_sleep()
    return out


# ---------------------------------------------------------------------------
# Naukri (India's largest board). Server-rendered result cards; no login,
# no CAPTCHA in normal use. India-only, so it skips other countries honestly.
# ---------------------------------------------------------------------------
def _naukri_card(card, role):
    """Map one Naukri result tuple to a job dict, or None if unusable."""
    a = card.select_one("a.title")
    href = a.get("href") if a else ""
    if not a or not href:
        return None
    title = a.get_text(" ", strip=True)
    if not title:
        return None
    link = href if href.startswith("http") else _NAUKRI_BASE + href

    company = _txt(card, "a.comp-name") or _txt(card, "span.comp-name")
    location = _txt(card, "span.locWdth")
    lpa, disp = _salary_if_numeric(_txt(card, "span.sal-wrap"))

    # Stable id: the data-attr, else the trailing numeric segment of the link.
    job_id = card.get("data-job-id") or ""
    if not job_id:
        tail = href.rstrip("/").split("-")[-1]
        job_id = tail if tail.isdigit() else href

    return {
        "source": "Naukri",
        "company": company,
        "title": title,
        "job_id": str(job_id),
        "job_link": link,
        "location": location,
        "salary": disp,
        "salary_lpa": lpa,
        "remote": "remote" in location.lower(),
        "match_score": cvprofile.match_score(title, [role]),
        "job_description": "",   # full JD is on the posting page; keep -> Unknown
        "_validated": True,      # live SERP render is the validation
    }


def _naukri_is_india(country):
    c = (country or "").lower()
    return c.startswith("india") or c in ("", "worldwide")


def _naukri(driver, roles, country):
    """Scrape Naukri for each role (India only). Card presence is the block
    signal: a challenge/empty page yields no cards and we stop honestly."""
    if not _naukri_is_india(country):
        utils.info(f"Naukri covers India only - skipping it for '{country}'.")
        return []
    out = []
    for role in roles:
        slug = _slug(role) or "job"
        for page in range(1, config.HEADLESS_MAX_PAGES + 1):
            url = f"{_NAUKRI_BASE}/{slug}-jobs"
            if page > 1:
                url += f"-{page}"
            if not headless.load(driver, url,
                                 wait_for="div.srp-jobtuple-wrapper"):
                break
            cards = (headless.soup(driver).select("div.srp-jobtuple-wrapper")
                     or headless.soup(driver).select("[data-job-id]"))
            if not cards:
                break
            got = [j for j in (_naukri_card(c, role) for c in cards) if j]
            out.extend(got)
            utils.info(f"Naukri '{role}' page {page}: {len(got)} job(s).")
            utils.polite_sleep()
    return out


# ---------------------------------------------------------------------------
# Wellfound (startup / tech jobs). The /role/<slug> pages render headless
# WITHOUT a login — job links are /jobs/<id>-<slug>; the company is the nearest
# ancestor's name. (The logged-in /jobs feed hangs the renderer, so we don't
# use it.) Results are largely remote/global, so downstream filters do the rest.
# ---------------------------------------------------------------------------
def _wf_company_for(anchor):
    """Best-effort company: nearest ancestor holding a /company/ link or an h2."""
    node = anchor
    for _ in range(6):
        node = node.parent
        if node is None:
            break
        comp = node.select_one("a[href^='/company/']") or node.find("h2")
        if comp:
            txt = comp.get_text(" ", strip=True)
            if txt:
                return txt
    return ""


def _wf_card(anchor, role):
    """Map one Wellfound job anchor to a job dict, or None if unusable."""
    href = anchor.get("href", "")
    if "/jobs/" not in href:
        return None
    tail = href.split("/jobs/")[-1]
    job_id = tail.split("-")[0]
    if not job_id.isdigit():
        return None
    link = href if href.startswith("http") else _WF_BASE + href
    title = anchor.get_text(" ", strip=True)
    if not title:  # derive a title from the slug when the anchor has no text
        slug = "-".join(tail.split("-")[1:])
        title = slug.replace("-", " ").title()
    if not title:
        return None
    return {
        "source": "Wellfound",
        "company": _wf_company_for(anchor),
        "title": title,
        "job_id": job_id,
        "job_link": link,
        "location": "",
        "salary": "",
        "salary_lpa": None,
        "remote": False,
        "match_score": cvprofile.match_score(title, [role]),
        "job_description": "",
        "_validated": True,
    }


def _wellfound(driver, roles, country):
    """Scrape Wellfound /role/<slug> for each role; dedupe by job id within the
    run. Global results (startup roles), so country isn't part of the URL."""
    out, seen = [], set()
    for role in roles:
        slug = _slug(role)
        if not slug:
            continue
        if not headless.load(driver, f"{_WF_BASE}/role/{slug}",
                             wait_for="a[href*='/jobs/']"):
            continue
        if headless.looks_blocked(driver):
            utils.warn(f"Wellfound looks blocked for '{role}' - stopping it.")
            continue
        # Nudge lazy-loading so more than the first screen of jobs renders.
        for _ in range(2):
            try:
                driver.execute_script(
                    "window.scrollTo(0, document.body.scrollHeight);")
            except Exception:  # noqa: BLE001
                break
            time.sleep(2)
        got = 0
        for a in headless.soup(driver).select("a[href*='/jobs/']"):
            j = _wf_card(a, role)
            if not j or j["job_id"] in seen:
                continue
            seen.add(j["job_id"])
            out.append(j)
            got += 1
        utils.info(f"Wellfound '{role}': {got} job(s).")
        utils.polite_sleep()
    return out


# ---------------------------------------------------------------------------
# Glassdoor. Hostile to headless: a 'normal' page load hangs the renderer, so
# the shared browser uses pageLoadStrategy='eager' (see headless.py). Also, the
# page embeds the word 'captcha' in its JSON even on good pages, so the generic
# looks_blocked() FALSE-positives here — instead, card presence is the ground
# truth (a real challenge renders no jobListing cards -> honest skip). Note:
# JSearch also aggregates Glassdoor, so this is a redundant-but-direct fallback.
# ---------------------------------------------------------------------------
def _gd_loc_params(country):
    c = (country or "").lower()
    if c.startswith("india"):
        return {"locT": "N", "locId": "115"}
    if "united states" in c or c in ("us", "usa"):
        return {"locT": "N", "locId": "1"}
    return {}  # remote / worldwide — no location filter


def _gd_card(card, role):
    """Map one Glassdoor jobListing card to a job dict, or None if unusable."""
    a = card.select_one("a[data-test=job-title]")
    href = a.get("href") if a else ""
    if not a or not href:
        return None
    title = a.get_text(" ", strip=True)
    if not title:
        return None
    link = href if href.startswith("http") else _GD_BASE + href

    comp = card.select_one("[class*=EmployerProfile_compactEmployerName]")
    company = comp.get_text(" ", strip=True) if comp else ""
    location = _txt(card, "[data-test=emp-location]")
    lpa, disp = _salary_if_numeric(_txt(card, "[data-test=detailSalary]"))

    job_id = card.get("data-jobid") or ""
    if not job_id and "jl=" in href:
        job_id = href.split("jl=")[-1].split("&")[0]

    return {
        "source": "Glassdoor",
        "company": company,
        "title": title,
        "job_id": str(job_id or link),
        "job_link": link,
        "location": location,
        "salary": disp,
        "salary_lpa": lpa,
        "remote": "remote" in location.lower(),
        "match_score": cvprofile.match_score(title, [role]),
        "job_description": "",
        "_validated": True,
    }


def _glassdoor(driver, roles, country):
    """Scrape Glassdoor's keyword search (page 1 per role — its pagination is a
    server-side URL rewrite that doesn't survive a simple query param). Card
    presence is the block signal; no fabrication on a challenge page."""
    loc = _gd_loc_params(country)
    out = []
    for role in roles:
        params = dict(loc, **{"sc.keyword": role})
        url = _GD_BASE + "/Job/jobs.htm?" + urllib.parse.urlencode(params)
        if not headless.load(driver, url,
                             wait_for="li[data-test=jobListing]"):
            continue
        cards = headless.soup(driver).select("li[data-test=jobListing]")
        if not cards:
            utils.info(f"Glassdoor '{role}': no cards (no results or a "
                       f"challenge page) - skipping honestly.")
            continue
        got = [j for j in (_gd_card(c, role) for c in cards) if j]
        out.extend(got)
        utils.info(f"Glassdoor '{role}': {len(got)} job(s).")
        utils.polite_sleep()
    return out


# ---------------------------------------------------------------------------
# Dispatcher — key must match a config.SCRAPERS toggle (case-insensitive).
# ---------------------------------------------------------------------------
_ADAPTERS = {
    "SimplyHired": _simplyhired,
    "Naukri": _naukri,
    "Wellfound": _wellfound,
    "Glassdoor": _glassdoor,
}


def fetch(roles, country):
    """
    Scrape all ENABLED headless portals for the given roles + country. Opens ONE
    browser for the whole batch. Returns a deduped list of job dicts (each with
    `_validated=True`). Honest-skip (returns []) if nothing is enabled or the
    browser is unavailable — never fabricates, never crashes.
    """
    enabled = [name for name in _ADAPTERS
               if config.SCRAPERS.get(name.lower(), False)]
    if not enabled:
        return []

    ok, reason = headless.available()
    if not ok:
        utils.warn(f"Headless-scraped portals selected but {reason} Skipping.")
        return []

    headed = config.HEADLESS_MODE.strip().lower() == "headed"
    # A persistent profile only matters for the human-login portals (headed).
    profile = config.HEADLESS_PROFILE_DIR if headed else None
    utils.step(f"Scraping {len(enabled)} portal(s) via {'headed' if headed else 'headless'} "
               f"Chrome for '{country}': {', '.join(enabled)}")

    all_jobs = []
    with headless.browser(headless=not headed, profile_dir=profile) as driver:
        if driver is None:
            return []  # framework already narrated the reason
        for name in enabled:
            try:
                got = _ADAPTERS[name](driver, roles, country)
                utils.info(f"{name}: {len(got)} raw job(s).")
                all_jobs.extend(got)
            except Exception as e:  # noqa: BLE001
                utils.warn(f"{name} scraper hit an error ({e}) - skipping it, "
                           f"continuing with the rest.")
            utils.polite_sleep()

    deduped = utils.dedupe(all_jobs)
    utils.ok(f"Headless-scraped total after dedupe: {len(deduped)} job(s).")
    return deduped
