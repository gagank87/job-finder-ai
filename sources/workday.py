"""
Workday source — per-tenant public CXS API.

Workday is hosted per-company: each employer has its own subdomain and
"career site" board name, so we can't guess it from a company name. Instead
you paste the company's Workday careers URL, e.g.:

    https://salesforce.wd12.myworkdayjobs.com/External_Career_Site
    https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite

From that we call the public endpoint:
    POST https://{host}/wday/cxs/{tenant}/{board}/jobs
and build canonical links as:
    https://{host}/{board}{externalPath}

All IDs (JR…) and links come straight from Workday — never fabricated.
"""

import re
from urllib.parse import urlparse

import config
import cvprofile
import salary as salarymod
import utils


def parse_workday_url(url):
    """
    Return (host, tenant, board) from a pasted Workday URL, or (None, None, None).

    host   e.g. salesforce.wd12.myworkdayjobs.com
    tenant e.g. salesforce   (subdomain before .wdNN)
    board  e.g. External_Career_Site  (first path segment)
    """
    u = url.strip()
    if "myworkdayjobs.com" not in u and "myworkdaysite.com" not in u:
        return None, None, None
    parsed = urlparse(u if "//" in u else "https://" + u)
    host = parsed.netloc
    if not host:
        return None, None, None
    # tenant = leftmost subdomain label
    tenant = host.split(".")[0]
    # board = first non-empty path segment, skipping locale codes like en-US
    segs = [s for s in parsed.path.split("/") if s]
    board = None
    for s in segs:
        if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", s):  # locale like en-US
            continue
        board = s
        break
    if not board:
        return None, None, None
    return host, tenant, board


def _salary_from_bullets(bullet_fields):
    """Workday sometimes puts a pay string in bulletFields; try to parse it."""
    for b in bullet_fields or []:
        if any(sym in str(b) for sym in ("$", "₹", "€", "£")) or \
           re.search(r"\bLPA\b|per year|per hour|/yr|/hr", str(b), re.I):
            lpa, disp = salarymod.normalize(raw=str(b))
            if lpa is not None:
                return lpa, disp
    return None, ""


def fetch_from_url(url, company_label, roles):
    """
    Fetch relevant Workday jobs for one pasted URL.
    Returns a list of normalized job dicts (links NOT yet validated).
    """
    host, tenant, board = parse_workday_url(url)
    if not host:
        utils.warn("That doesn't look like a Workday URL "
                   "(expected …myworkdayjobs.com/…). Skipping.")
        return []

    utils.info(f"Workday tenant '{tenant}', board '{board}'.")
    api = f"https://{host}/wday/cxs/{tenant}/{board}/jobs"
    session = utils.get_session()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    out = []
    for role in roles:
        offset = 0
        while True:
            try:
                r = session.post(api, json={"limit": 20, "offset": offset,
                                            "searchText": role},
                                 headers=headers, timeout=config.HTTP_TIMEOUT)
            except Exception as e:  # noqa: BLE001
                utils.warn(f"Workday request failed: {e}")
                break
            if r.status_code != 200:
                break
            data = r.json()
            postings = data.get("jobPostings", [])
            if not postings:
                break
            for p in postings:
                ext = p.get("externalPath", "")
                jid = (p.get("bulletFields") or [""])[0] or ext.rsplit("_", 1)[-1]
                title = p.get("title", "")
                lpa, disp = _salary_from_bullets(p.get("bulletFields"))
                out.append({
                    "source": f"Workday ({company_label})",
                    "company": company_label,
                    "title": title,
                    "job_id": jid,
                    "job_link": f"https://{host}/{board}{ext}",
                    "location": p.get("locationsText", ""),
                    "posted": p.get("postedOn", ""),
                    "salary": disp,
                    "salary_lpa": lpa,
                    "remote": "remote" in (p.get("locationsText", "").lower()),
                    "match_score": cvprofile.match_score(title, roles),
                })
            offset += len(postings)
            if offset >= data.get("total", 0) or offset >= 100:
                break
            utils.polite_sleep()

    # keep only role-relevant titles
    relevant = [j for j in out if j["match_score"] >= 40]
    deduped = utils.dedupe(relevant)
    utils.ok(f"Workday '{company_label}': {len(deduped)} relevant job(s).")
    return deduped
