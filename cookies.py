"""
Optional cookie-injection layer.

You can drop a `cookies.json` next to this file mapping a domain to a
cookie string copied from your logged-in browser, e.g.:

    {
      "linkedin.com": "li_at=XXXX; JSESSIONID=\"ajax:123\"",
      "instahyre.com": "sessionid=YYYY"
    }

Where it genuinely helps:
  * LinkedIn  — an `li_at` session unlocks more results and sometimes the
    hiring-team/recruiter link that the logged-out guest API hides.

Where it does NOT help (documented honestly):
  * Cloudflare-gated sites (Indeed, Wellfound, Glassdoor, Dice, SimplyHired)
    and reCAPTCHA sites (Naukri). Their gate is tied to your browser's TLS +
    JavaScript fingerprint, not a cookie, so a pasted cookie won't get past
    them from a plain Python request. Those remain out of scope for now.

How to get the LinkedIn cookie:
  Log in on the browser → DevTools (F12) → Application → Cookies →
  https://www.linkedin.com → copy the `li_at` value → put
  "linkedin.com": "li_at=<value>" in cookies.json.
"""

import json
import os

import config
import utils


def _parse_cookie_string(cookie_str):
    """Turn 'a=1; b=2' into {'a': '1', 'b': '2'}."""
    jar = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            jar[k.strip()] = v.strip()
    return jar


def load_into_session(path=None):
    """
    Load cookies.json (if present) and attach each domain's cookies to the
    shared requests session. Returns the list of domains loaded (for
    narration). Safe no-op if the file is missing or malformed.
    """
    path = path or config.COOKIES_FILE
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        utils.warn(f"Could not read {path}: {e} (continuing without cookies).")
        return []

    session = utils.get_session()
    loaded = []
    for domain, cookie_str in data.items():
        if not cookie_str:
            continue
        jar = _parse_cookie_string(cookie_str)
        for k, v in jar.items():
            # set on both the bare and dot domain so subdomains match
            session.cookies.set(k, v, domain=domain)
            session.cookies.set(k, v, domain="." + domain.lstrip("."))
        loaded.append(domain)
    return loaded


def has_linkedin_auth():
    """
    True if a LinkedIn `li_at` session cookie is loaded in the shared session.

    Used to decide whether to attempt fetching the (login-only) hiring-team
    profile link. Without it we don't even try — no wasted requests.
    """
    try:
        session = utils.get_session()
        return any(c.name == "li_at" for c in session.cookies)
    except Exception:  # noqa: BLE001
        return False
