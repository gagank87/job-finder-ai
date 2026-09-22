"""
LinkedIn source — public guest jobs API (no login required).

Uses the same endpoint LinkedIn's own public job-search page calls:
    https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search

Every job ID and link returned here is REAL, parsed straight from
LinkedIn's HTML response. Links are the canonical hrefs LinkedIn itself
emits (…/jobs/view/<slug>-<id>), so clicking one opens that exact posting.
No links are ever fabricated.
"""

import re
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import config
import cookies
import cvprofile
import utils

GUEST_SEARCH_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)

# Canonical logged-in job page (carries the "Meet the hiring team" module).
_JOB_VIEW_URL = "https://www.linkedin.com/jobs/view/{job_id}"
_PROFILE_HREF_RE = re.compile(r"^https?://[^/]*linkedin\.com/in/[^/?#]+")

_URN_RE = re.compile(r"urn:li:jobPosting:(\d+)")
_ID_IN_URL_RE = re.compile(r"-(\d+)(?:\?|$)")


def _extract_job_id(card) -> str:
    """Prefer the entity URN; fall back to the numeric id in the href."""
    urn = card.get("data-entity-urn", "")
    m = _URN_RE.search(urn)
    if m:
        return m.group(1)
    link = card.find("a", class_="base-card__full-link")
    if link and link.get("href"):
        m = _ID_IN_URL_RE.search(link["href"].split("?")[0] + "?")
        if m:
            return m.group(1)
    return ""


def _canonical_link(card, job_id: str) -> str:
    """
    Use the real href LinkedIn emitted (stripped of tracking params).
    Fall back to the stable /jobs/view/<id> form only if no href exists.
    """
    link = card.find("a", class_="base-card__full-link")
    if link and link.get("href"):
        return link["href"].split("?")[0]
    if job_id:
        return f"https://www.linkedin.com/jobs/view/{job_id}"
    return ""


def _parse_cards(html: str, roles: list[str]) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.find_all("div", class_="base-card")
    jobs = []
    for card in cards:
        job_id = _extract_job_id(card)
        if not job_id:
            continue

        title_el = card.find("h3", class_="base-search-card__title")
        title = title_el.get_text(strip=True) if title_el else ""

        company_el = card.find("h4", class_="base-search-card__subtitle")
        company = company_el.get_text(strip=True) if company_el else ""

        loc_el = card.find("span", class_="job-search-card__location")
        location = loc_el.get_text(strip=True) if loc_el else ""

        time_el = card.find("time")
        posted = time_el.get("datetime", "") if time_el else ""

        jobs.append({
            "source": "LinkedIn",
            "company": company,
            "title": title,
            "job_id": job_id,
            "job_link": _canonical_link(card, job_id),
            "hiring_team_link": "",  # not exposed via guest API (see plan)
            "location": location,
            "posted": posted,
            "salary": "",            # LinkedIn guest cards don't expose pay
            "salary_lpa": None,
            "remote": "remote" in location.lower(),
            "match_score": cvprofile.match_score(title, roles),
        })
    return jobs


def _clean_profile_url(href: str) -> str:
    """Normalize a /in/ profile href to a clean canonical URL, or ""."""
    if not href:
        return ""
    href = urljoin("https://www.linkedin.com", href)
    if not _PROFILE_HREF_RE.match(href):
        return ""
    return href.split("?")[0].rstrip("/")


def _fetch_hiring_team_link(job_id: str) -> str:
    """
    Fetch the authenticated LinkedIn job page and return the REAL profile URL
    of the hiring-team member, if the posting exposes one. "" otherwise.

    Only meaningful with an `li_at` cookie loaded — the "Meet the hiring team"
    module is login-only. Never raises; never fabricates a link (returns the
    exact /in/ href LinkedIn emits, or nothing).
    """
    jid = str(job_id).strip()
    if not jid.isdigit():
        return ""
    session = utils.get_session()
    try:
        r = session.get(_JOB_VIEW_URL.format(job_id=jid),
                        timeout=config.HTTP_TIMEOUT, allow_redirects=True)
    except Exception:  # noqa: BLE001
        return ""
    if r.status_code != 200 or not r.text:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")

    # Prefer the explicit hiring-team card; fall back to any /in/ profile link
    # inside a container that mentions hiring. Both read the real emitted href.
    card = soup.find(class_=re.compile(r"hiring-team|hirer-card|job-details-people"))
    search_root = card if card else soup
    for a in search_root.find_all("a", href=True):
        url = _clean_profile_url(a["href"])
        if url:
            return url
    return ""


def _enrich_hiring_team(jobs: list[dict]) -> None:
    """
    Populate hiring_team_link for LinkedIn jobs, in place, when authenticated.

    No-op (leaves the field blank) without an li_at cookie, so we never waste
    a request or fabricate data. Honest by construction.
    """
    if not cookies.has_linkedin_auth():
        return
    utils.step("Looking up hiring-team profile links (LinkedIn, logged in)")
    found = 0
    for job in jobs:
        link = _fetch_hiring_team_link(job.get("job_id", ""))
        if link:
            job["hiring_team_link"] = link
            found += 1
        utils.polite_sleep()
    utils.ok(f"Hiring-team link found for {found}/{len(jobs)} LinkedIn job(s).")


def _get_with_backoff(session, params):
    """
    GET the guest search endpoint, retrying on a 429 with an escalating wait.

    A 429 is transient throttling, not a dead end — we pause (honoring a
    Retry-After header when present, else an escalating base wait) and retry the
    SAME request. Returns the response on any non-429 outcome (caller inspects
    the status), or None if we exhausted retries / the request kept failing.
    """
    for attempt in range(config.LINKEDIN_MAX_RETRIES + 1):
        try:
            resp = session.get(GUEST_SEARCH_URL, params=params,
                               timeout=config.HTTP_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            utils.warn(f"Request failed: {e}")
            return None

        if resp.status_code != 429:
            return resp

        if attempt >= config.LINKEDIN_MAX_RETRIES:
            return None  # out of retries — let the caller stop LinkedIn

        # Work out how long to wait: prefer the server's Retry-After, else an
        # escalating base wait (20s, 40s, 60s...), capped.
        wait = config.LINKEDIN_BACKOFF_SECONDS * (attempt + 1)
        retry_after = resp.headers.get("Retry-After")
        if retry_after and str(retry_after).strip().isdigit():
            wait = int(retry_after)
        wait = min(wait, config.LINKEDIN_BACKOFF_MAX_SECONDS)
        utils.warn(f"LinkedIn rate-limited us (429) — waiting {wait}s then "
                   f"retrying (attempt {attempt + 1}/"
                   f"{config.LINKEDIN_MAX_RETRIES}).")
        time.sleep(wait)

    return None


def fetch(search_terms: list[str], cities: list[str],
          exp_codes) -> list[dict]:
    """
    Fetch LinkedIn jobs for every (search-term x city x experience-level)
    combination, restricted to the last 24 hours.

    `search_terms` are the keywords to search (roles plus any level-specific
    extras like "apprenticeship" / "management trainee").
    `exp_codes` is a list of LinkedIn f_E codes (one run can span several
    experience levels); pass an empty list / [""] for no level filter.

    Returns a de-duplicated list of job dicts (links NOT yet validated).
    Dedupe by job_id absorbs the overlap when levels/terms return the same job.
    """
    session = utils.get_session()
    all_jobs: list[dict] = []

    # Normalize experience codes: accept a str, a list, or empty.
    if isinstance(exp_codes, str):
        exp_codes = [exp_codes]
    codes = [c for c in (exp_codes or []) if c] or [""]

    # Once LinkedIn throttles us past our retries, further queries would only
    # dig the hole deeper — so we stop LinkedIn for the rest of the run (keeping
    # whatever we already fetched) instead of hammering every remaining query.
    throttled_out = False

    for city in cities:
        if throttled_out:
            break
        for role in search_terms:
            if throttled_out:
                break
            for exp_code in codes:
                lvl = f"exp level {exp_code}" if exp_code else "any level"
                utils.step(
                    f"Searching LinkedIn: '{role}' in {city} "
                    f"(posted last 24h, {lvl})"
                )
                found_for_query = 0
                for page in range(config.LINKEDIN_MAX_PAGES):
                    params = {
                        "keywords": role,
                        "location": city,
                        "f_TPR": config.LINKEDIN_TPR_24H,
                        "start": page * 10,
                    }
                    if exp_code:
                        params["f_E"] = exp_code

                    resp = _get_with_backoff(session, params)
                    if resp is None:
                        # Persistent 429 even after backoff: stop LinkedIn for
                        # this run, but keep everything gathered so far.
                        utils.warn("LinkedIn kept rate-limiting us despite "
                                   "backing off — stopping LinkedIn for this "
                                   "run and keeping what we already fetched. "
                                   "(Tip: fewer countries/cities/roles per run, "
                                   "or try again in a few minutes.)")
                        throttled_out = True
                        break
                    if resp.status_code != 200:
                        utils.warn(f"Unexpected status {resp.status_code}; "
                                   f"stopping this query.")
                        break

                    page_jobs = _parse_cards(resp.text, search_terms)
                    if not page_jobs:
                        break  # no more results
                    all_jobs.extend(page_jobs)
                    found_for_query += len(page_jobs)
                    utils.polite_sleep()

                utils.ok(f"{found_for_query} raw result(s) for '{role}' in {city}.")
                if throttled_out:
                    break

    deduped = utils.dedupe(all_jobs)
    utils.ok(f"LinkedIn total after dedupe: {len(deduped)} job(s).")

    # When logged in (li_at cookie), enrich with the real hiring-team profile
    # link. Skipped silently otherwise — the column just stays blank.
    _enrich_hiring_team(deduped)
    return deduped
