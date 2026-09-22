"""
JSearch source — real postings from the major job portals via one legit API.

JSearch (RapidAPI, https://jsearch.p.rapidapi.com) is a legitimate aggregator
that returns REAL, currently-listed jobs from Indeed, Glassdoor, ZipRecruiter,
Monster, Google Jobs, LinkedIn and more, each with its canonical apply link.
This is how those major portals are covered in this tool WITHOUT scraping,
CAPTCHAs, or IP bans (all of which make direct scraping of those sites
unreliable and, in several cases, against their terms).

It needs a free RapidAPI key. If no key is configured (JSEARCH_API_KEY env var
or secrets/jsearch_key.txt), this source SKIPS honestly with a one-line
instruction — it never fabricates results and never crashes the run. Every
job_id / apply link returned here comes straight from the API response.
"""

import config
import cvprofile
import salary as salarymod
import utils

# OpenWeb Ninja (the JSearch publisher) retired the old page-based /search
# endpoint; /search-v2 is the current one. It wraps results as
# {"data": {"jobs": [...], "cursor": "..."}} and paginates by cursor, not page.
_SEARCH_URL = f"https://{config.JSEARCH_HOST}/search-v2"


def _headers(key):
    return {"X-RapidAPI-Key": key, "X-RapidAPI-Host": config.JSEARCH_HOST}


def _country_code(country):
    """Map a chosen country label to JSearch's 2-letter country param."""
    c = (country or "").lower()
    if c.startswith("india"):
        return "in"
    if "united states" in c or c == "usa" or c == "us":
        return "us"
    # Remote / Worldwide and anything else: default to US market, remote-only.
    return "us"


def _apply_link(j):
    """Prefer the canonical apply link; fall back to any provided option URL."""
    link = j.get("job_apply_link") or ""
    if link:
        return link
    for opt in (j.get("apply_options") or []):
        if opt.get("apply_link"):
            return opt["apply_link"]
    return j.get("job_google_link") or ""


def _location(j):
    parts = [j.get("job_city"), j.get("job_state"), j.get("job_country")]
    loc = ", ".join(p for p in parts if p)
    if j.get("job_is_remote"):
        loc = (loc + ", Remote").strip(", ") if loc else "Remote"
    return loc or ""


def _currency_for(j):
    """v2 dropped job_salary_currency, so infer it from the job's country
    (falling back to USD). Keeps INR salaries from being scaled as dollars."""
    if j.get("job_salary_currency"):        # tolerate it if the API adds it back
        return j["job_salary_currency"]
    return {"IN": "INR", "US": "USD", "GB": "GBP",
            "CA": "CAD", "AU": "AUD"}.get(
        (j.get("job_country") or "").upper(), "USD")


def _mk(j, roles):
    title = j.get("job_title", "")
    publisher = j.get("job_publisher", "")  # underlying board (Indeed, Glassdoor…)
    src = f"JSearch/{publisher}" if publisher else "JSearch"
    lpa, disp = salarymod.normalize(
        min_amt=j.get("job_min_salary"),
        max_amt=j.get("job_max_salary"),
        currency=_currency_for(j),
        period=(j.get("job_salary_period") or "year"))
    return {
        "source": src,
        "company": j.get("employer_name", "") or "",
        "title": title,
        "job_id": str(j.get("job_id", "")),
        "job_link": _apply_link(j),
        "location": _location(j),
        "salary": disp,
        "salary_lpa": lpa,
        "remote": bool(j.get("job_is_remote")),
        "match_score": cvprofile.match_score(title, roles),
        # Carry the JD inline so the eligibility analyzer reuses it for free.
        "job_description": j.get("job_description", "") or "",
    }


def _jobs_and_cursor(payload):
    """Pull the jobs list and next-page cursor out of a v2 response, tolerating
    the legacy bare-list `data` shape too (so the tool survives either)."""
    data = payload.get("data")
    if isinstance(data, list):          # legacy /search shape
        return data, None
    if isinstance(data, dict):          # current /search-v2 shape
        return (data.get("jobs") or []), data.get("cursor")
    return [], None


def _search_one(role, country_code, remote_only, key):
    """One JSearch query for a single role; returns raw job dicts (unvalidated).
    /search-v2 paginates by an opaque cursor, so we thread it page to page."""
    s = utils.get_session()
    out = []
    cursor = None
    for _page in range(config.JSEARCH_MAX_PAGES):
        params = {
            "query": role,
            "country": country_code,
            "date_posted": "week",
        }
        if cursor:
            params["cursor"] = cursor
        if remote_only:
            params["work_from_home"] = "true"
        try:
            r = s.get(_SEARCH_URL, params=params, headers=_headers(key),
                      timeout=config.HTTP_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            utils.warn(f"JSearch request failed: {e}")
            break
        if r.status_code == 429:
            utils.warn("JSearch rate-limited/quota exhausted (429) — stopping "
                       "JSearch for this run.")
            break
        if r.status_code in (401, 403):
            utils.warn("JSearch rejected the API key (check JSEARCH_API_KEY).")
            break
        if r.status_code == 404:
            # /search-v2 is the current endpoint; a 404 here means the key's
            # RapidAPI app isn't subscribed to JSearch (OpenWeb Ninja). Fix is
            # on the RapidAPI account (Subscribe to the free Basic plan), not
            # in the code.
            utils.warn("JSearch returned 404. Your RapidAPI key's app is likely "
                       "NOT subscribed to JSearch — open the JSearch API "
                       "(publisher: OpenWeb Ninja) on rapidapi.com, click "
                       "'Subscribe' on the free Basic plan, and use that key.")
            break
        if r.status_code != 200:
            utils.warn(f"JSearch returned status {r.status_code}; stopping.")
            break
        jobs, cursor = _jobs_and_cursor(r.json())
        if not jobs:
            break
        out.extend(jobs)
        if not cursor:          # no further pages
            break
        utils.polite_sleep()
    return out


def fetch(roles, country):
    """
    Fetch real postings from Indeed/Glassdoor/ZipRecruiter/Monster/Google Jobs
    (etc.) via JSearch, for each target role. Returns a deduped, normalized list.

    Skips honestly (returns []) with a clear instruction if no API key is set —
    never fabricates, never crashes.
    """
    key = config.get_jsearch_key()
    if not key:
        utils.warn("JSearch source is selected but no API key is set - skipping "
                   "it. To enable Indeed/Glassdoor/ZipRecruiter/Monster/Google "
                   "Jobs, get a free key at rapidapi.com (search 'JSearch') and "
                   "set JSEARCH_API_KEY, or put it in "
                   f"{config.JSEARCH_API_KEY_FILE}.")
        return []

    cc = _country_code(country)
    remote_only = (country or "").lower().startswith("remote")
    utils.step(f"Searching JSearch (Indeed/Glassdoor/ZipRecruiter/Monster/"
               f"Google Jobs) for {len(roles)} role(s) in '{country}'")
    raw = []
    for role in roles:
        got = _search_one(role, cc, remote_only, key)
        utils.info(f"JSearch '{role}': {len(got)} raw result(s).")
        raw.extend(got)
        utils.polite_sleep()

    jobs = [_mk(j, roles) for j in raw if j.get("job_title")]
    # Keep only jobs with a real apply link (never a blank/fabricated one).
    jobs = [j for j in jobs if j["job_link"]]
    deduped = utils.dedupe(jobs)
    utils.ok(f"JSearch total after dedupe: {len(deduped)} job(s).")
    return deduped
