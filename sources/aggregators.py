"""
Remote-job aggregators with public JSON APIs (no login).

Adapters, one per board:
  * RemoteOK    https://remoteok.com/api
  * Remotive    https://remotive.com/api/remote-jobs
  * Arbeitnow   https://www.arbeitnow.com/api/job-board-api
  * Himalayas   https://himalayas.app/jobs/api   (has salary)
  * Jobicy      https://jobicy.com/api/v2/remote-jobs  (has salary)
  * WorkingNomads https://www.workingnomads.com/api/exposed_jobs/
  * Jobspresso  https://jobspresso.co/?post_type=job_listing&feed=rss2 (RSS)

These boards list ALL remote jobs; we filter to your target roles (via the
CV match score) and to your chosen country / remote preference. Every job's
id and link come straight from the API — never fabricated.
"""

import cvprofile
import config
import salary as salarymod
import utils


def _relevant(title, roles):
    return cvprofile.match_score(title, roles) >= 45


def _country_ok(location_text, country, remote_flag):
    """
    Keep if: the job is remote/worldwide, OR its location mentions the chosen
    country. Lenient by design (per user's 'include country + remote' choice).
    """
    if country.lower().startswith("remote"):
        return True
    loc = (location_text or "").lower()
    if remote_flag or "worldwide" in loc or "anywhere" in loc or "remote" in loc:
        return True
    # crude country / demonym match
    c = country.lower()
    if c in loc:
        return True
    aliases = {
        "united states": ["usa", "u.s", "us,", "united states", "america"],
        "united kingdom": ["uk", "united kingdom", "england", "britain"],
        "united arab emirates": ["uae", "dubai", "abu dhabi"],
    }
    return any(a in loc for a in aliases.get(c, []))


def _mk(source, company, title, jid, link, location, roles,
        salary_lpa=None, salary_disp="", remote=True, desc=""):
    return {
        "source": source,
        "company": company,
        "title": title,
        "job_id": str(jid),
        "job_link": link,
        "location": location,
        "salary": salary_disp,
        "salary_lpa": salary_lpa,
        "remote": remote,
        "match_score": cvprofile.match_score(title, roles),
        # JD text carried inline where the API provides it, so the JD
        # analyzer can reuse it without a second request (jdfetch.get_jd).
        "job_description": desc or "",
    }


def _remoteok(roles, country):
    s = utils.get_session()
    r = s.get("https://remoteok.com/api", timeout=config.HTTP_TIMEOUT)
    if r.status_code != 200:
        return []
    out = []
    for j in r.json():
        if "position" not in j:  # skip the legal/meta header element
            continue
        title = j.get("position", "")
        if not _relevant(title, roles):
            continue
        loc = j.get("location", "") or "Remote"
        if not _country_ok(loc, country, True):
            continue
        lpa, disp = salarymod.normalize(
            min_amt=j.get("salary_min") or None,
            max_amt=j.get("salary_max") or None,
            currency="USD", period="year")
        out.append(_mk("RemoteOK", j.get("company", ""), title, j.get("id", ""),
                       j.get("url", ""), loc, roles, lpa, disp, True))
    return out


def _remotive(roles, country):
    s = utils.get_session()
    r = s.get("https://remotive.com/api/remote-jobs",
              params={"limit": 500}, timeout=config.HTTP_TIMEOUT)
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("jobs", []):
        title = j.get("title", "")
        if not _relevant(title, roles):
            continue
        loc = j.get("candidate_required_location", "") or "Remote"
        if not _country_ok(loc, country, True):
            continue
        lpa, disp = salarymod.normalize(raw=j.get("salary", ""))
        out.append(_mk("Remotive", j.get("company_name", ""), title,
                       j.get("id", ""), j.get("url", ""), loc, roles,
                       lpa, disp, True, desc=j.get("description", "")))
    return out


def _arbeitnow(roles, country):
    s = utils.get_session()
    r = s.get("https://www.arbeitnow.com/api/job-board-api",
              timeout=config.HTTP_TIMEOUT)
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("data", []):
        title = j.get("title", "")
        if not _relevant(title, roles):
            continue
        loc = j.get("location", "") or ""
        remote = bool(j.get("remote"))
        if not _country_ok(loc, country, remote):
            continue
        out.append(_mk("Arbeitnow", j.get("company_name", ""), title,
                       j.get("slug", ""), j.get("url", ""), loc, roles,
                       None, "", remote, desc=j.get("description", "")))
    return out


def _himalayas(roles, country):
    s = utils.get_session()
    r = s.get("https://himalayas.app/jobs/api",
              params={"limit": 100}, timeout=config.HTTP_TIMEOUT)
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("jobs", []):
        title = j.get("title", "")
        if not _relevant(title, roles):
            continue
        locs = j.get("locationRestrictions") or []
        loc = ", ".join(locs) if locs else "Remote"
        if not _country_ok(loc, country, True):
            continue
        lpa, disp = salarymod.normalize(
            min_amt=j.get("minSalary"), max_amt=j.get("maxSalary"),
            currency=(j.get("currency") or "USD"),
            period=(j.get("salaryPeriod") or "year"))
        out.append(_mk("Himalayas", j.get("companyName", ""), title,
                       j.get("guid", ""), j.get("applicationLink", ""),
                       loc, roles, lpa, disp, True,
                       desc=j.get("description", "")))
    return out


def _jobicy(roles, country):
    s = utils.get_session()
    r = s.get("https://jobicy.com/api/v2/remote-jobs",
              params={"count": 100}, timeout=config.HTTP_TIMEOUT)
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("jobs", []):
        title = j.get("jobTitle", "")
        if not _relevant(title, roles):
            continue
        loc = j.get("jobGeo", "") or "Remote"
        if not _country_ok(loc, country, True):
            continue
        lpa, disp = salarymod.normalize(
            min_amt=j.get("salaryMin"), max_amt=j.get("salaryMax"),
            currency=(j.get("salaryCurrency") or "USD"),
            period=(j.get("salaryPeriod") or "year"))
        out.append(_mk("Jobicy", j.get("companyName", ""), title,
                       j.get("id", ""), j.get("url", ""), loc, roles,
                       lpa, disp, True, desc=j.get("jobDescription", "")))
    return out


def _strip_html(text):
    """Turn an HTML description into readable plain text (best-effort)."""
    if not text:
        return ""
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    except Exception:  # noqa: BLE001
        return str(text)


def _workingnomads(roles, country):
    """
    Working Nomads — public JSON of currently-listed remote jobs.
    https://www.workingnomads.com/api/exposed_jobs/
    Every url/title/company comes straight from the feed; nothing fabricated.
    """
    s = utils.get_session()
    r = s.get("https://www.workingnomads.com/api/exposed_jobs/",
              timeout=config.HTTP_TIMEOUT)
    if r.status_code != 200:
        return []
    out = []
    for j in r.json():
        title = j.get("title", "")
        if not _relevant(title, roles):
            continue
        loc = j.get("location", "") or "Remote"
        if not _country_ok(loc, country, True):
            continue
        url = j.get("url", "")
        # Use the URL's trailing id as a stable job_id when present.
        jid = url.rstrip("/").rsplit("/", 1)[-1] or url
        out.append(_mk("WorkingNomads", j.get("company_name", ""), title,
                       jid, url, loc, roles, None, "", True,
                       desc=_strip_html(j.get("description", ""))))
    return out


def _jobspresso(roles, country):
    """
    Jobspresso — remote jobs via the WordPress job_listing RSS feed.
    https://jobspresso.co/?post_type=job_listing&feed=rss2
    Links are the real <link> hrefs the feed emits.
    """
    s = utils.get_session()
    r = s.get("https://jobspresso.co/",
              params={"post_type": "job_listing", "feed": "rss2"},
              timeout=config.HTTP_TIMEOUT)
    if r.status_code != 200:
        return []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "xml")
    except Exception:  # noqa: BLE001
        return []
    out = []
    for item in soup.find_all("item"):
        title_raw = (item.title.get_text(strip=True) if item.title else "")
        link = (item.link.get_text(strip=True) if item.link else "")
        if not title_raw or not link:
            continue
        # Feed titles are usually "Job Title at Company"; split for a company.
        if " at " in title_raw:
            title, company = title_raw.rsplit(" at ", 1)
        else:
            title, company = title_raw, ""
        if not _relevant(title, roles):
            continue
        # Jobspresso is remote-only; keep unless the user picked a specific
        # country the listing clearly excludes (feed rarely states location).
        if not _country_ok("Remote", country, True):
            continue
        desc_el = item.find("description")
        desc = _strip_html(desc_el.get_text() if desc_el else "")
        jid = link.rstrip("/").rsplit("/", 1)[-1] or link
        out.append(_mk("Jobspresso", company.strip(), title.strip(),
                       jid, link, "Remote", roles, None, "", True, desc=desc))
    return out


_ADAPTERS = {
    "RemoteOK": _remoteok,
    "Remotive": _remotive,
    "Arbeitnow": _arbeitnow,
    "Himalayas": _himalayas,
    "Jobicy": _jobicy,
    "WorkingNomads": _workingnomads,
    "Jobspresso": _jobspresso,
}


def fetch(roles, country):
    """Run all aggregator adapters; return a deduped, normalized list."""
    utils.step(f"Remote aggregators (filtering to '{country}' + remote)")
    all_jobs = []
    for name, fn in _ADAPTERS.items():
        try:
            jobs = fn(roles, country)
        except Exception as e:  # noqa: BLE001
            utils.warn(f"{name} failed: {e}")
            jobs = []
        utils.info(f"{name}: {len(jobs)} relevant.")
        all_jobs.extend(jobs)
        utils.polite_sleep()
    deduped = utils.dedupe(all_jobs)
    utils.ok(f"Aggregators total after dedupe: {len(deduped)} job(s).")
    return deduped
