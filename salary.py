"""
Salary normalization + the hard >= 8 LPA filter.

Job sources report pay in wildly different shapes: explicit min/max numbers
with a currency + period (aggregators), or free-text strings, or nothing at
all. This module normalizes whatever we can into annual INR (lakhs), and
applies the rule:

    KEEP a job iff  salary is unknown (None)  OR  annual >= SALARY_FLOOR_LPA

Jobs with an explicitly stated salary clearly below the floor are dropped.
Internship / trainee titles are NEVER used to infer low pay — only a stated
number drops a job.
"""

import re

import config

# Multipliers to bring a per-period figure to annual.
_PERIOD_TO_ANNUAL = {
    "year": 1, "yr": 1, "annum": 1, "annual": 1, "pa": 1, "p.a": 1,
    "month": 12, "mo": 12, "monthly": 12,
    "week": 52, "wk": 52, "weekly": 52,
    "day": 260, "daily": 260,
    "hour": 2080, "hr": 2080, "hourly": 2080,
}

_CURRENCY_SIGNS = {
    "₹": "INR", "rs": "INR", "inr": "INR",
    "$": "USD", "usd": "USD",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
    "cad": "CAD", "aud": "AUD", "sgd": "SGD", "aed": "AED",
}


def _to_lpa(amount_inr):
    """Convert an absolute INR annual figure to lakhs-per-annum."""
    return amount_inr / 100_000.0


def _num(text):
    """Parse a number that may use k/lakh/lac/crore/cr/m suffixes."""
    t = text.lower().replace(",", "").strip()
    m = re.match(r"([\d.]+)\s*([a-z]*)", t)
    if not m:
        return None
    val = float(m.group(1))
    suf = m.group(2)
    if suf.startswith("k"):
        val *= 1_000
    elif suf.startswith(("lakh", "lac", "l")):
        val *= 100_000
    elif suf.startswith(("crore", "cr")):
        val *= 10_000_000
    elif suf.startswith("m"):
        val *= 1_000_000
    return val


def normalize(raw=None, min_amt=None, max_amt=None, currency=None, period=None):
    """
    Return (lpa_or_None, display_string).

    Two calling styles:
      * structured: pass min_amt/max_amt (+ currency, period) from an API.
      * free-text: pass raw string; we do best-effort parsing.
    lpa_or_None is the annual figure in lakhs INR (uses the MAX of a range),
    or None when nothing usable is found.
    """
    # --- structured path (aggregators) ---
    if min_amt or max_amt:
        cur = (currency or "USD").upper()
        rate = config.FX_TO_INR.get(cur, config.FX_TO_INR["USD"])
        per = (period or "year").lower()
        mult = _PERIOD_TO_ANNUAL.get(per, 1)
        vals = [v for v in (min_amt, max_amt) if v]
        if not vals:
            return None, ""
        top = max(float(v) for v in vals) * mult * rate
        lpa = _to_lpa(top)
        lo = f"{min_amt:g}" if min_amt else ""
        hi = f"{max_amt:g}" if max_amt else ""
        disp = f"{cur} {lo}-{hi} /{per}".replace("- ", "").strip()
        return round(lpa, 1), disp

    # --- free-text path ---
    if not raw or not str(raw).strip():
        return None, ""
    text = str(raw)
    low = text.lower()

    # detect currency
    cur = "INR"
    for sign, code in _CURRENCY_SIGNS.items():
        if sign in low:
            cur = code
            break
    # detect period
    per = "year"
    for p in _PERIOD_TO_ANNUAL:
        if re.search(rf"\b{re.escape(p)}\b", low) or f"/{p}" in low or f"per {p}" in low:
            per = p
            break

    # find numbers (with optional suffix), take the largest as the "max"
    nums = re.findall(r"[\d][\d,]*\.?\d*\s*(?:k|lakh|lac|l|crore|cr|m)?", low)
    parsed = [n for n in (_num(x) for x in nums) if n]
    if not parsed:
        return None, text.strip()

    top = max(parsed)
    rate = config.FX_TO_INR.get(cur, 1.0)
    mult = _PERIOD_TO_ANNUAL.get(per, 1)
    lpa = _to_lpa(top * mult * rate)
    return round(lpa, 1), text.strip()


def passes_floor(lpa):
    """
    The hard rule: keep if unknown (None) OR >= floor.
    """
    if lpa is None:
        return True
    return lpa >= config.SALARY_FLOOR_LPA


def apply_filter(jobs):
    """
    Filter a list of job dicts by the salary rule. Each job may carry a
    'salary_lpa' (float or None) and 'salary' (display). Returns
    (kept_jobs, dropped_count).
    """
    kept = []
    dropped = 0
    for j in jobs:
        if passes_floor(j.get("salary_lpa")):
            kept.append(j)
        else:
            dropped += 1
    return kept, dropped
