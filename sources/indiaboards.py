"""
India-focused job boards with public JSON APIs (no login).

  * Instahyre  https://www.instahyre.com/api/v1/job_search
  * Unstop     https://unstop.com/api/public/opportunity/search-result

Both list many jobs; we filter to your target roles via the CV match score.
IDs and links come straight from each API — never fabricated.
"""

import cvprofile
import config
import utils


def _relevant(title, roles):
    return cvprofile.match_score(title, roles) >= 45


def _instahyre(roles):
    s = utils.get_session()
    out = []
    for role in roles:
        try:
            r = s.get("https://www.instahyre.com/api/v1/job_search",
                      params={"limit": 50, "q": role},
                      headers={"Accept": "application/json"},
                      timeout=config.HTTP_TIMEOUT)
        except Exception:  # noqa: BLE001
            continue
        if r.status_code != 200:
            continue
        for j in r.json().get("objects", []):
            title = j.get("title", "") or j.get("candidate_title", "")
            if not _relevant(title, roles):
                continue
            emp = j.get("employer") or {}
            company = emp.get("company_name", "") if isinstance(emp, dict) else ""
            locs = j.get("locations") or []
            loc = ", ".join(locs) if isinstance(locs, list) else str(locs)
            out.append({
                "source": "Instahyre",
                "company": company,
                "title": title,
                "job_id": str(j.get("id", "")),
                "job_link": j.get("public_url", ""),
                "location": loc or "India",
                "salary": "",
                "salary_lpa": None,   # not exposed in list view
                "remote": "remote" in loc.lower(),
                "match_score": cvprofile.match_score(title, roles),
            })
        utils.polite_sleep()
    return out


def _unstop(roles):
    s = utils.get_session()
    out = []
    for role in roles:
        try:
            r = s.get("https://unstop.com/api/public/opportunity/search-result",
                      params={"opportunity": "jobs", "per_page": 30,
                              "searchText": role},
                      headers={"Accept": "application/json"},
                      timeout=config.HTTP_TIMEOUT)
        except Exception:  # noqa: BLE001
            continue
        if r.status_code != 200:
            continue
        for j in r.json().get("data", {}).get("data", []):
            title = j.get("title", "")
            if not _relevant(title, roles):
                continue
            org = j.get("organisation") or {}
            company = org.get("name", "") if isinstance(org, dict) else ""
            pub = j.get("public_url", "")
            link = pub if pub.startswith("http") else f"https://unstop.com/{pub}"
            out.append({
                "source": "Unstop",
                "company": company,
                "title": title,
                "job_id": str(j.get("id", "")),
                "job_link": link,
                "location": "India",
                "salary": "",
                "salary_lpa": None,
                "remote": False,
                "match_score": cvprofile.match_score(title, roles),
            })
        utils.polite_sleep()
    return out


def fetch(roles):
    """Run Instahyre + Unstop; return a deduped, normalized list."""
    utils.step("India boards (Instahyre, Unstop)")
    all_jobs = []
    for name, fn in (("Instahyre", _instahyre), ("Unstop", _unstop)):
        try:
            jobs = fn(roles)
        except Exception as e:  # noqa: BLE001
            utils.warn(f"{name} failed: {e}")
            jobs = []
        utils.info(f"{name}: {len(jobs)} relevant.")
        all_jobs.extend(jobs)
    deduped = utils.dedupe(all_jobs)
    utils.ok(f"India boards total after dedupe: {len(deduped)} job(s).")
    return deduped
