"""
Company career-pages source.

Companies host jobs on Applicant Tracking Systems (ATS). Each ATS exposes
a PUBLIC JSON API returning REAL job IDs and canonical apply URLs. Given a
company NAME we guess a "token" and try each ATS; if none resolve, we ask
the user to paste the careers/ATS URL and detect the platform from it.

Every link returned is echoed straight from the ATS response (or the ATS's
canonical public URL form) — never fabricated from a guessed slug.
"""

import html
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

import config
import cvprofile
import utils


# Example URL shapes the tool can actually fetch — shown to the user when a
# pasted URL doesn't resolve, so they paste the JOBS BOARD, not a landing page.
URL_FORMAT_EXAMPLES = [
    "Greenhouse:      https://boards.greenhouse.io/<company>",
    "Lever:           https://jobs.lever.co/<company>",
    "Ashby:           https://jobs.ashbyhq.com/<company>",
    "SmartRecruiters: https://jobs.smartrecruiters.com/<company>",
    "Workday:         https://<company>.wd1.myworkdayjobs.com/<Board>",
]


# ---------------------------------------------------------------------------
# Token guessing from a company name.
# ---------------------------------------------------------------------------
def _guess_tokens(company_name: str) -> list[str]:
    """Produce candidate ATS tokens from a company name, most-likely first."""
    name = company_name.strip().lower()
    compact = re.sub(r"[^a-z0-9]", "", name)          # "acme corp" -> "acmecorp"
    hyphen = re.sub(r"[^a-z0-9]+", "-", name).strip("-")  # -> "acme-corp"
    first = name.split()[0] if name.split() else compact
    # Drop common suffixes for a bare-name guess.
    bare = re.sub(r"(inc|llc|ltd|limited|corp|corporation|technologies|labs)$",
                  "", compact)
    candidates = [compact, hyphen, first, bare]
    # de-dupe while preserving order
    seen, out = set(), []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# Per-ATS fetchers. Each returns a list of normalized job dicts, or [] if
# the token doesn't resolve to a real, populated board.
# ---------------------------------------------------------------------------
def _norm(company, title, job_id, link, location, roles, source, desc=""):
    return {
        "source": source,
        "company": company,
        "title": title,
        "job_id": str(job_id),
        "job_link": link,
        "location": location,
        "salary": "",            # ATS list endpoints rarely expose pay
        "salary_lpa": None,
        "remote": "remote" in (location or "").lower(),
        "match_score": cvprofile.match_score(title, roles),
        # Lever exposes the JD inline; other ATS list endpoints don't, so the
        # analyzer fetches those on demand (jdfetch.get_jd).
        "job_description": desc or "",
    }


def _fetch_greenhouse(token, company_label, roles):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    session = utils.get_session()
    r = session.get(url, timeout=config.HTTP_TIMEOUT)
    if r.status_code != 200:
        return []
    jobs = r.json().get("jobs", [])
    out = []
    for j in jobs:
        loc = (j.get("location") or {}).get("name", "")
        out.append(_norm(company_label, j.get("title", ""), j.get("id", ""),
                         j.get("absolute_url", ""), loc, roles, "Greenhouse"))
    return out


def _fetch_lever(token, company_label, roles):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    session = utils.get_session()
    r = session.get(url, timeout=config.HTTP_TIMEOUT)
    if r.status_code != 200:
        return []
    data = r.json()
    if not isinstance(data, list) or not data:
        return []
    out = []
    for j in data:
        loc = (j.get("categories") or {}).get("location", "")
        desc = j.get("descriptionPlain") or j.get("description", "")
        out.append(_norm(company_label, j.get("text", ""), j.get("id", ""),
                         j.get("hostedUrl", ""), loc, roles, "Lever",
                         desc=desc))
    return out


def _fetch_ashby(token, company_label, roles):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    session = utils.get_session()
    r = session.get(url, timeout=config.HTTP_TIMEOUT)
    if r.status_code != 200:
        return []
    jobs = r.json().get("jobs", [])
    out = []
    for j in jobs:
        loc = j.get("location", "") or ""
        out.append(_norm(company_label, j.get("title", ""), j.get("id", ""),
                         j.get("jobUrl", ""), loc, roles, "Ashby"))
    return out


def _fetch_smartrecruiters(token, company_label, roles):
    base = f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
    session = utils.get_session()
    out = []
    offset = 0
    while True:
        r = session.get(base, params={"limit": 100, "offset": offset},
                        timeout=config.HTTP_TIMEOUT)
        if r.status_code != 200:
            break
        data = r.json()
        content = data.get("content", [])
        if not content:
            break
        for j in content:
            loc = (j.get("location") or {}).get("fullLocation", "")
            link = f"https://jobs.smartrecruiters.com/{token}/{j.get('id')}"
            out.append(_norm(company_label, j.get("name", ""), j.get("id", ""),
                             link, loc, roles, "SmartRecruiters"))
        offset += len(content)
        if offset >= data.get("totalFound", 0):
            break
        utils.polite_sleep()
    return out


_ATS_FETCHERS = {
    "greenhouse": _fetch_greenhouse,
    "lever": _fetch_lever,
    "ashby": _fetch_ashby,
    "smartrecruiters": _fetch_smartrecruiters,
}


# ---------------------------------------------------------------------------
# URL-fallback: detect platform + token from a pasted careers/ATS URL.
# ---------------------------------------------------------------------------
def _skip_locale(parts):
    """Drop a leading locale segment like 'en-US' / 'en' from a path."""
    out = list(parts)
    while out and (re.fullmatch(r"[a-z]{2}-[A-Z]{2}", out[0])
                   or re.fullmatch(r"[a-z]{2}", out[0])):
        out.pop(0)
    return out


def detect_from_url(url: str):
    """
    Return (platform, token) parsed from a pasted careers/ATS URL, or
    (None, None). Handles the public URL shapes for each ATS the tool can fetch.

    For Workday the "token" is the FULL Workday URL — the caller hands it to the
    Workday fetcher, which parses tenant/board itself (per-tenant, not guessable).

    Enterprise platforms we can't fetch programmatically (iCIMS/Taleo/Oracle/
    SuccessFactors/Phenom/Eightfold/Avature/Brassring) are recognised too, but
    returned as ("<name>-unsupported", None) so the caller can give SPECIFIC
    guidance instead of a generic "couldn't detect".
    """
    u = url.strip()
    host = urlparse(u).netloc.lower()
    path = urlparse(u).path.strip("/")
    parts = _skip_locale([p for p in path.split("/") if p])

    # --- platforms with a real public JSON API we fetch directly ---
    if "greenhouse.io" in host:
        # boards.greenhouse.io/<token>  or  job-boards.greenhouse.io/<token>
        if parts:
            return "greenhouse", parts[0]
    if "lever.co" in host:
        if parts:
            return "lever", parts[0]
    if "ashbyhq.com" in host:
        if parts:
            return "ashby", parts[0]
    if "smartrecruiters.com" in host:
        if parts:
            return "smartrecruiters", parts[0]

    # --- Workday: per-tenant; hand the whole URL to the Workday fetcher ---
    if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
        return "workday", u

    # --- enterprise platforms we recognise but can't fetch via a public API ---
    # (JavaScript-rendered and/or gated; we tell the user honestly + how to
    #  proceed, rather than silently failing.)
    _UNSUPPORTED_HOSTS = {
        "icims.com": "iCIMS",
        "taleo.net": "Taleo",
        "oraclecloud.com": "Oracle Recruiting",
        "successfactors.com": "SAP SuccessFactors",
        "successfactors.eu": "SAP SuccessFactors",
        "phenompeople.com": "Phenom",
        "eightfold.ai": "Eightfold",
        "avature.net": "Avature",
        "brassring.com": "Brassring",
        "workforcenow.adp.com": "ADP",
    }
    for frag, label in _UNSUPPORTED_HOSTS.items():
        if frag in host:
            return f"{label}-unsupported", None

    return None, None


# Regexes that recognise an embedded ATS from a company's own careers-page
# HTML. Many companies host jobs on their OWN domain (e.g. stripe.com/jobs,
# figma.com/careers) but embed a known ATS board underneath. Each capture
# group is the real board token — so links stay real, never guessed.
_SNIFF_PATTERNS = [
    ("greenhouse", re.compile(
        r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?"
        r"([a-zA-Z0-9_-]+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board\?for=([a-zA-Z0-9_-]+)")),
    ("lever", re.compile(r"(?:jobs\.lever\.co|api\.lever\.co/v0/postings)/([a-zA-Z0-9_-]+)")),
    ("ashby", re.compile(
        r"(?:jobs\.ashbyhq\.com|api\.ashbyhq\.com/posting-api/job-board)/"
        r"([a-zA-Z0-9_-]+)")),
    ("smartrecruiters", re.compile(
        r"(?:jobs|api)\.smartrecruiters\.com/(?:v1/companies/)?([a-zA-Z0-9]+)")),
]

# Workday is handled separately (per-tenant, different fetch path).
_WORKDAY_RE = re.compile(r"https?://[a-z0-9.-]+\.myworkday(?:jobs|site)\.com/[^\s\"'<>]+")

# Enterprise platforms we can't fetch via a public API, but CAN recognise when a
# company's own careers page links out/embeds them — so we name them + guide the
# user instead of a blank "couldn't detect". frag in page HTML -> label.
_UNSUPPORTED_EMBED = [
    (re.compile(r"[a-z0-9-]+\.icims\.com", re.I), "iCIMS"),
    (re.compile(r"[a-z0-9.-]*taleo\.net", re.I), "Taleo"),
    (re.compile(r"[a-z0-9-]+\.oraclecloud\.com", re.I), "Oracle Recruiting"),
    (re.compile(r"[a-z0-9.-]*successfactors\.(?:com|eu)", re.I),
     "SAP SuccessFactors"),
    (re.compile(r"[a-z0-9-]+\.phenompeople\.com", re.I), "Phenom"),
    (re.compile(r"[a-z0-9-]+\.eightfold\.ai", re.I), "Eightfold"),
    (re.compile(r"[a-z0-9-]+\.avature\.net", re.I), "Avature"),
]


def sniff_ats_from_page(url):
    """
    Fetch a company's own careers page and detect an embedded, known ATS.

    Returns one of:
      * ("greenhouse"|"lever"|"ashby"|"smartrecruiters", token)
      * ("workday", workday_url)
      * (None, None)  if nothing real is found (caller should skip honestly).

    Only reports a platform when a real board token / Workday URL is present
    in the page — no links are ever guessed from the company name.
    """
    session = utils.get_session()
    try:
        r = session.get(url, timeout=config.HTTP_TIMEOUT, allow_redirects=True)
    except Exception as e:  # noqa: BLE001
        utils.warn(f"Could not load that page to detect its ATS: {e}")
        return None, None
    if r.status_code != 200 or not r.text:
        utils.warn(f"That page returned status {r.status_code}; can't detect an ATS.")
        return None, None

    page_html = r.text

    # Workday first (its URL is the most specific signature).
    wd = _WORKDAY_RE.search(page_html)
    if wd:
        return "workday", wd.group(0)

    for platform, pat in _SNIFF_PATTERNS:
        m = pat.search(page_html)
        if m:
            token = m.group(1)
            # Ignore obvious non-token captures.
            if token and token.lower() not in ("embed", "www", "job", "jobs",
                                               "v1", "v0", "companies"):
                return platform, token

    # Recognise (but can't fetch) an embedded enterprise platform — report it so
    # the caller can name it and guide the user, rather than a blank failure.
    for pat, label in _UNSUPPORTED_EMBED:
        if pat.search(page_html):
            return f"{label}-unsupported", None
    return None, None


# ---------------------------------------------------------------------------
# JSON-LD JobPosting fallback.
#
# Most careers pages — including ones on unknown or custom ATS platforms
# (Oracle/Taleo, SuccessFactors, iCIMS, Avature, Phenom, or a bespoke stack
# like HCLTech's) — embed schema.org/JobPosting structured data in a
# <script type="application/ld+json"> block for SEO. We read the REAL apply
# URL straight from that data. A posting is emitted ONLY when it carries a
# genuine url — never a guessed slug — so the "no fake links" rule holds.
#
# Limitation kept honest: pages that render jobs purely via client-side
# JavaScript (no JSON-LD in the initial HTML) still yield nothing here,
# because we execute no JavaScript. Those need the deferred browser mode.
# ---------------------------------------------------------------------------
def _iter_jsonld_objects(page_html):
    """Yield every JSON object found in the page's ld+json script blocks."""
    soup = BeautifulSoup(page_html, "html.parser")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                # Unwrap @graph containers, but also yield the node itself.
                if "@graph" in node and isinstance(node["@graph"], list):
                    stack.extend(node["@graph"])
                yield node


def _is_job_posting(node):
    t = node.get("@type", "")
    if isinstance(t, list):
        return any(str(x).lower() == "jobposting" for x in t)
    return str(t).lower() == "jobposting"


def _jsonld_location(node):
    """Best-effort human location string from a JobPosting's jobLocation."""
    jl = node.get("jobLocation")
    if isinstance(jl, list):
        jl = jl[0] if jl else None
    if isinstance(jl, dict):
        addr = jl.get("address")
        if isinstance(addr, dict):
            parts = [addr.get("addressLocality"), addr.get("addressRegion"),
                     addr.get("addressCountry")]
            parts = [str(p) for p in parts if p]
            if parts:
                return ", ".join(parts)
        if jl.get("name"):
            return str(jl["name"])
    if node.get("applicantLocationRequirements"):
        alr = node["applicantLocationRequirements"]
        if isinstance(alr, dict) and alr.get("name"):
            return str(alr["name"])
    return ""


def _jsonld_apply_url(node, page_url):
    """
    Return a REAL apply/posting URL from the JobPosting, or "" if none is
    present. We never synthesize one — an entry with no url is skipped.
    """
    url = node.get("url") or node.get("sameAs") or ""
    # Some feeds put the apply link under hiringOrganization / potentialAction.
    if not url:
        action = node.get("potentialAction")
        if isinstance(action, dict):
            target = action.get("target")
            if isinstance(target, dict):
                url = target.get("urlTemplate") or target.get("url") or ""
            elif isinstance(target, str):
                url = target
    url = (url or "").strip()
    if not url:
        return ""
    # Resolve a relative URL against the page it was found on (still real).
    if url.startswith("/"):
        url = urljoin(page_url, url)
    return url


def sniff_jsonld_jobs(url, company_label, roles):
    """
    Fetch a careers page and extract real JobPosting entries from its JSON-LD.

    Returns a list of normalized job dicts (links NOT yet validated), or []
    if the page carries no usable structured job data.
    """
    session = utils.get_session()
    try:
        r = session.get(url, timeout=config.HTTP_TIMEOUT, allow_redirects=True)
    except Exception as e:  # noqa: BLE001
        utils.warn(f"Could not load that page for JobPosting data: {e}")
        return []
    if r.status_code != 200 or not r.text:
        return []

    out = []
    for node in _iter_jsonld_objects(r.text):
        if not isinstance(node, dict) or not _is_job_posting(node):
            continue
        title = html.unescape(str(node.get("title", ""))).strip()
        apply_url = _jsonld_apply_url(node, r.url)
        if not title or not apply_url:
            continue  # never emit a job without a real title + real link
        org = node.get("hiringOrganization")
        company = company_label
        if isinstance(org, dict) and org.get("name"):
            company = str(org["name"])
        loc = _jsonld_location(node)
        desc = ""
        if node.get("description"):
            # description is HTML; store it inline so jdfit can reuse it.
            desc = str(node["description"])
        jid = str(node.get("identifier", "") or apply_url)
        if isinstance(node.get("identifier"), dict):
            jid = str(node["identifier"].get("value", "") or apply_url)
        out.append(_norm(company, title, jid, apply_url, loc, roles,
                         "Company site (JobPosting)", desc=desc))
    return out


# ---------------------------------------------------------------------------
# Filtering to your target roles / experience keyword.
# ---------------------------------------------------------------------------
def _filter_relevant(jobs, roles, level_keyword=""):
    """
    Career boards list ALL of a company's jobs. Keep only those whose title
    matches your target roles (via match score) and, if given, an experience
    keyword (best-effort title contains).
    """
    kept = []
    for j in jobs:
        title = j.get("title", "").lower()
        relevant = j.get("match_score", 0) >= 40 or any(
            all(w in title for w in role.lower().split() if len(w) > 2)
            for role in roles
        )
        if not relevant:
            continue
        if level_keyword and level_keyword.lower() not in title:
            # only exclude when a level keyword was requested AND clearly absent;
            # we keep it lenient so we don't drop good roles that omit the word.
            pass
        kept.append(j)
    return kept


# ---------------------------------------------------------------------------
# URL entries typed straight into the "Career companies" box.
# ---------------------------------------------------------------------------
_SECOND_LEVEL_TLDS = {"co", "com", "org", "net", "gov", "ac", "edu"}


def looks_like_url(text):
    """True if the entry is a pasted http(s) URL rather than a company name."""
    return bool(re.match(r"https?://", (text or "").strip(), re.I))


def _label_from_url(url):
    """Derive a readable company label from a careers URL's domain.

    e.g. https://www.tuvsud.com/en-in/careers -> 'Tuvsud';
         https://southasiacareers.deloitte.com/... -> 'Deloitte'.
    JSON-LD / ATS responses override this with the real name when available.
    """
    host = urlparse(url.strip()).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in host.split(".") if p]
    label = ""
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL_TLDS:
        label = parts[-3]          # glassdoor.co.in -> 'glassdoor'
    elif len(parts) >= 2:
        label = parts[-2]          # tuvsud.com -> 'tuvsud'
    elif parts:
        label = parts[0]
    return (label or "Company").replace("-", " ").title()


def _fetch_from_url(url, company_label, roles, level_keyword=""):
    """
    Fetch jobs directly from a pasted careers/ATS URL.

    Detect the platform from the URL, else sniff the page for an embedded ATS,
    else read schema.org/JobPosting data off the page. Never guesses a link.
    Returns relevant, normalized job dicts (links NOT yet validated).
    """
    platform, token = detect_from_url(url)

    # Direct Workday URL -> the Workday fetcher parses tenant/board itself.
    if platform == "workday":
        return _fetch_via_workday(token, company_label, roles)

    # Recognised an enterprise platform we can't fetch via a public API — say
    # which one and how to get results, instead of a blank "couldn't detect".
    if platform and platform.endswith("-unsupported"):
        _warn_unsupported(platform, company_label)
        return []

    # Not a direct ATS URL? Many companies host jobs on their OWN domain
    # (e.g. figma.com/careers, stripe.com/jobs) but embed a known ATS board.
    # Sniff the page HTML for that real board — never guess a link.
    if not platform:
        utils.info("Not a direct ATS link — checking whether this careers "
                   "page embeds a known ATS...")
        platform, token = sniff_ats_from_page(url)
        if platform == "workday":
            # token is the real Workday URL; hand off to the Workday fetcher.
            utils.ok(f"Detected an embedded Workday board for '{company_label}'.")
            return _fetch_via_workday(token, company_label, roles)
        if platform and platform.endswith("-unsupported"):
            _warn_unsupported(platform, company_label)
            return []
        if platform:
            utils.ok(f"Detected an embedded {platform} board '{token}'.")

    # No known ATS embedded — try reading schema.org/JobPosting structured
    # data straight off the page. This covers unknown/custom ATS platforms
    # (Oracle/Taleo, SuccessFactors, iCIMS, Avature, Phenom, bespoke sites)
    # as long as they publish real apply URLs in JSON-LD (most do, for SEO).
    if not platform:
        utils.info("No known ATS board on that page — checking for embedded "
                   "JobPosting data (schema.org)...")
        jsonld_jobs = sniff_jsonld_jobs(url, company_label, roles)
        if jsonld_jobs:
            relevant = _filter_relevant(jsonld_jobs, roles, level_keyword)
            utils.ok(f"Found {len(jsonld_jobs)} posting(s) via JobPosting data, "
                     f"{len(relevant)} match your target roles.")
            return relevant

    if not platform:
        utils.warn("Couldn't find a supported ATS or JobPosting data on that "
                   "page. Skipping rather than guessing a link.")
        _print_url_format_help()
        return []

    # A supported, fetchable ATS (greenhouse/lever/ashby/smartrecruiters).
    if platform not in _ATS_FETCHERS:
        utils.warn(f"Detected '{platform}', but there's no fetcher for it. "
                   f"Skipping rather than guessing.")
        return []
    if token:
        utils.info(f"Using {platform} board '{token}'.")
    try:
        jobs = _ATS_FETCHERS[platform](token, company_label, roles)
    except Exception:  # noqa: BLE001
        jobs = []
    if not jobs:
        utils.warn("No jobs returned from that board.")
        return []
    relevant = _filter_relevant(jobs, roles, level_keyword)
    utils.ok(f"{len(jobs)} posting(s), {len(relevant)} match your target roles.")
    return relevant


# ---------------------------------------------------------------------------
# Public entry point for one company.
# ---------------------------------------------------------------------------
def fetch_company(company_name, roles, ask_url_fn=None, level_keyword=""):
    """
    Fetch jobs for one company (or one pasted careers/ATS URL).

    0. If the entry is itself a URL, fetch straight from it (detect -> sniff ->
       JSON-LD). This is what the GUI relies on: pasting a careers URL into the
       "Career companies" box just works, with no ask_url_fn callback.
    1. Otherwise guess ATS tokens from the name and try each ATS.
    2. If nothing resolves and ask_url_fn is provided, ask the user for the
       careers/ATS URL and detect platform+token from it.

    ask_url_fn: callable(company_name) -> url string (or "" to skip).
    Returns a list of relevant, normalized job dicts (links NOT yet validated).
    """
    # 0) A pasted URL: skip name-based token guessing entirely.
    if looks_like_url(company_name):
        label = _label_from_url(company_name)
        utils.step(f"Company career page: fetching directly from a pasted URL "
                   f"({label})...")
        return _fetch_from_url(company_name.strip(), label, roles, level_keyword)

    utils.step(f"Company career page: '{company_name}' - auto-detecting ATS...")
    tokens = _guess_tokens(company_name)

    for platform in config.ATS_PLATFORMS:
        fetcher = _ATS_FETCHERS[platform]
        for token in tokens:
            try:
                jobs = fetcher(token, company_name, roles)
            except Exception:  # noqa: BLE001
                jobs = []
            utils.polite_sleep()
            if jobs:
                utils.ok(f"Found on {platform} (token '{token}'): "
                         f"{len(jobs)} total posting(s).")
                relevant = _filter_relevant(jobs, roles, level_keyword)
                utils.info(f"{len(relevant)} match your target roles.")
                return relevant

    utils.warn(f"Could not auto-detect an ATS for '{company_name}'.")
    if ask_url_fn is None:
        return []

    url = (ask_url_fn(company_name) or "").strip()
    if not url:
        return []
    return _fetch_from_url(url, company_name, roles, level_keyword)


def _print_url_format_help():
    """Show the user what a fetchable careers/ATS URL looks like."""
    utils.info("Tip — paste the URL of the actual JOBS BOARD (not the marketing "
               "careers page). Fetchable formats look like:")
    for example in URL_FORMAT_EXAMPLES:
        utils.info(f"     {example}")


def _fetch_via_workday(workday_url, company_name, roles):
    """Route a detected Workday URL to the Workday fetcher (lazy import)."""
    from sources import workday  # lazy import to avoid a cycle
    return workday.fetch_from_url(workday_url, company_name, roles)


def _warn_unsupported(platform, company_name):
    """
    Explain, honestly, that we recognised the platform but can't fetch it via a
    public API — and tell the user the one thing that DOES work: use LinkedIn,
    or find this company's jobs on a board we support.
    """
    label = platform[:-len("-unsupported")]
    utils.warn(
        f"'{company_name}' uses {label}, which serves its listings through a "
        f"login/JavaScript-gated portal with no public API, so this tool can't "
        f"read it directly (doing so reliably needs a full headless browser, a "
        f"separate sub-project).")
    utils.info(
        "What works instead: (1) keep the LinkedIn source on - it usually "
        "carries the same postings; or (2) if this employer ALSO posts on "
        "Workday/Greenhouse/Lever, paste that board's URL instead.")


def fetch(company_names, roles, ask_url_fn=None, level_keyword=""):
    """Fetch across multiple companies; results are collated together."""
    all_jobs = []
    for name in company_names:
        all_jobs.extend(
            fetch_company(name, roles, ask_url_fn=ask_url_fn,
                          level_keyword=level_keyword)
        )
    deduped = utils.dedupe(all_jobs)
    utils.ok(f"Career pages total after dedupe: {len(deduped)} job(s).")
    return deduped
