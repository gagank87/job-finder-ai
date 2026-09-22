"""
Job Finder — interactive CLI (v3).

Fetches REAL job postings (real IDs + links, validated live) from:
  1. LinkedIn (posted last 24h)
  2. Company career pages (Greenhouse / Lever / Ashby / SmartRecruiters),
     including companies that embed one of these on their OWN domain
  3. Workday career pages (per-tenant, via pasted URL)
  4. Remote aggregators (RemoteOK, Remotive, Arbeitnow, Himalayas, Jobicy)
  5. India boards (Instahyre, Unstop)

You can pick several experience levels at once (e.g. Internship + Entry +
Associate); results are combined and each row is tagged with a Role type
(Internship / Apprenticeship / Trainee / Full-time). Internship searches also
look for apprenticeships; entry-level searches also look for management
trainees in marketing / data analytics. Senior/Sr/Lead/... titles are dropped
from early-career searches.

Standardized menu selection, salary floor filter (>= 8 LPA or unknown),
optional cookie injection, per-run Excel + a single growing master tracker
that dedupes across every run.

This file is a THIN interactive shell: it collects settings via menus and
delegates the actual fetch/validate/filter pipeline to core.run_search(),
which is also what the future scheduled/GUI modes will reuse (see interface.py).

Run:  python jobfinder.py
"""

import argparse
import json
import sys
from datetime import datetime

# Make the Windows console tolerate non-ASCII job titles / narration.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import config
import cookies
import core
import cvprofile
import excelio
import menus
import notify
import runcache
import settingsio
import tracker
import utils
from sources import careers


# ---------------------------------------------------------------------------
# Selection steps (menu-driven; standardized vocabulary).
# ---------------------------------------------------------------------------
def choose_roles():
    print("\n>> Target roles (from your CV analysis)")
    for i, role in enumerate(cvprofile.DEFAULT_ROLES, 1):
        print(f"   {i}. {role}")
    print("   These are drawn from your analytics/product background.")
    if menus.ask_yes_no("   Use these default roles?"):
        return list(cvprofile.DEFAULT_ROLES)
    raw = menus.ask_text("Enter roles, comma-separated: ")
    roles = [r.strip() for r in raw.split(",") if r.strip()]
    return roles or list(cvprofile.DEFAULT_ROLES)


def choose_experience():
    """
    Multi-select experience levels. One run can span several levels (e.g.
    Internship + Entry + Associate) — results are combined downstream.
    Returns a list of f_E codes.
    """
    opts = [(label, code) for _k, (label, code)
            in config.EXPERIENCE_LEVELS.items()]
    picked = menus.pick_many(
        "Experience level(s) — pick one or more (e.g. '1,2')",
        opts, default_all=False)
    if not picked:
        # Sensible default if the user just hits Enter: Entry level.
        picked = [config.EXPERIENCE_LEVELS["2"][1]]
    labels = [label for label, code in opts if code in picked]
    utils.ok(f"Experience level(s): {', '.join(labels)}")
    return picked


def choose_location():
    """
    Multi-select one or more countries; ask cities per country. Returns
    (countries_list, queries_list) where queries is the flattened list of
    "City, Country" (or bare "Country") location strings across all picks.
    """
    countries = menus.pick_many(
        "Location — country/countries (pick one or more, e.g. '1,2')",
        [(c, c) for c in config.COUNTRIES],
        default_all=False)
    if not countries:
        countries = [config.COUNTRIES[0]]  # default: first (India)

    queries = []
    for country in countries:
        if country.lower().startswith("remote"):
            queries.append(country)
            continue
        cities_raw = menus.ask_text(
            f"Cities in {country}, comma-separated (Enter = country-wide): ")
        cities = [c.strip() for c in cities_raw.split(",") if c.strip()]
        if cities:
            queries.extend(f"{c}, {country}" for c in cities)
        else:
            queries.append(country)

    utils.ok(f"Countries: {', '.join(countries)}")
    utils.ok(f"Will search: {', '.join(queries)}")
    return countries, queries


def choose_sources():
    opts = [(label, key) for key, (label, enabled)
            in config.SOURCES.items() if enabled]
    picked = menus.pick_many("Which sources? (multi-select)", opts,
                             default_all=True)
    utils.ok(f"Sources: {', '.join(picked) if picked else 'none'}")
    return set(picked)


def ask_companies(kind):
    raw = menus.ask_text(f"{kind} company name(s), comma-separated "
                         f"(Enter = skip): ")
    return [c.strip() for c in raw.split(",") if c.strip()]


def ask_url_for(company):
    print()
    print(f"   Couldn't auto-detect an ATS for '{company}'.")
    print("   Paste its JOBS BOARD url (not the marketing careers page). "
          "Fetchable formats:")
    for example in careers.URL_FORMAT_EXAMPLES:
        print(f"      {example}")
    print("   (Tip: on the company's careers page, click through to the list "
          "of open roles — that page's URL is the one to paste.)")
    return menus.ask_text(
        f"Paste the jobs-board URL for '{company}' (Enter = skip): ")


# ---------------------------------------------------------------------------
# QC gate.
# ---------------------------------------------------------------------------
def qc_preview(jobs):
    if not jobs:
        return False
    print("\n>> Quality check — please review the top 5 results by fit")
    # Rank by JD fit score when available, else the title match score.
    preview = sorted(
        jobs,
        key=lambda j: (j.get("fit_score", None) if j.get("fit_score") not in
                       (None, "") else j.get("match_score", 0)),
        reverse=True)[:5]
    for i, j in enumerate(preview, 1):
        sal = j.get("salary") or ("(not stated)" if j.get("salary_lpa") is None
                                  else f"{j.get('salary_lpa')} LPA")
        print(f"\n   [{i}] {j.get('title','')}  —  {j.get('company','')}")
        print(f"       Source : {j.get('source','')}   "
              f"Type: {j.get('role_type','')}   "
              f"Match: {j.get('match_score',0)}%   "
              f"Remote: {'Yes' if j.get('remote') else 'No'}")
        elig = j.get("eligibility", "")
        if elig:
            print(f"       Eligible: {elig}  (fit {j.get('fit_score','')}) "
                  f"— {j.get('fit_reason','')}")
        print(f"       Salary : {sal}    Location: {j.get('location','')}")
        print(f"       Job ID : {j.get('job_id','')}")
        print(f"       Link   : {j.get('job_link','')}")
    print("\n   Open a few links and confirm they open the exact, relevant "
          "postings.")
    return menus.ask_yes_no_strict(
        "   Do the links check out? Write to Excel + master tracker?",
        default_yes=True)


# ---------------------------------------------------------------------------
# Review + write (shared by a fresh run and a resumed-from-cache run).
# ---------------------------------------------------------------------------
def review_and_write(results):
    """
    Run the QC gate over `results` and, on approval, write the per-run workbook
    and merge into the master tracker. The in-progress cache is cleared either
    way — on a rejected QC (nothing written) and on a successful write — so it
    never lingers once the run is resolved.
    """
    linkedin_jobs = results["linkedin"]
    career_jobs = results["career"]
    other_jobs = results["other"]
    total = results.get("total", len(linkedin_jobs) + len(career_jobs)
                         + len(other_jobs))

    if total == 0:
        utils.warn("No live jobs found for your criteria. Nothing written.")
        utils.info("Tip: broaden the experience level, add cities, select more "
                   "sources, or try different roles.")
        runcache.clear()
        return

    utils.step(f"Found {total} live job(s): {len(linkedin_jobs)} LinkedIn, "
               f"{len(career_jobs)} career-page, {len(other_jobs)} other.")
    utils.info(f"Filtered out this run: {results.get('dropped_salary', 0)} on "
               f"salary, {results.get('dropped_senior', 0)} senior titles, "
               f"{results.get('dropped_duplicate', 0)} cross-source duplicates, "
               f"{results.get('dropped_tracked', 0)} already-tracked, "
               f"{results.get('dropped_ineligible', 0)} not-eligible (JD check).")

    if not qc_preview(linkedin_jobs + career_jobs + other_jobs):
        utils.warn("QC not approved — nothing written. Re-run when ready.")
        runcache.clear()   # requirement: drop the cache on a rejected QC
        return

    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Writing can fail if a target .xlsx is open in Excel (Windows locks it).
    # That's not fatal and must NOT cost the fetch: we keep the cache, tell the
    # user to close the file, and let them retry — the cache is cleared only
    # once the write actually succeeds.
    while True:
        try:
            path = excelio.write_workbook(linkedin_jobs, career_jobs,
                                          other_jobs, fetched_at)
            added, master_total, _new = tracker.merge(
                {"LinkedIn": linkedin_jobs,
                 "Career Pages": career_jobs,
                 "All Sources": other_jobs},
                fetched_at)
            break
        except PermissionError as e:
            locked = getattr(e, "filename", "") or "an output file"
            utils.warn(f"Couldn't write '{locked}' — it looks open in Excel "
                       f"(Windows locks open files).")
            utils.info("Your fetch is safe and still cached — nothing was lost.")
            if not menus.ask_yes_no_strict(
                    "   Close the file in Excel, then retry the save?",
                    default_yes=True):
                utils.warn("Save skipped. Relaunch anytime to resume from the "
                           "cached fetch.")
                return   # leave the cache in place so resume still works

    runcache.clear()   # requirement: drop the cache once data is in the master

    utils.step("Done!")
    utils.ok(f"This run saved to: {path}")
    utils.ok(f"Master tracker '{config.MASTER_TRACKER}': +{added} new job(s), "
             f"{master_total} total (no duplicates; your edits preserved).")


def offer_resume():
    """
    If an in-progress fetch was cached (a prior run that wasn't resolved),
    offer to jump straight back to its QC gate instead of re-fetching.

    Returns True if a cached run was resumed (caller should stop), else False
    (caller proceeds to a normal fresh run). A declined cache is cleared.
    """
    payload = runcache.load()
    if not payload:
        return False
    when = payload.get("fetched_at", "an earlier run")
    total = payload.get("total", 0)
    utils.step("A previous fetch was interrupted before you approved it.")
    utils.info(f"Saved {when} — {total} job(s), already fetched and filtered.")
    if menus.ask_yes_no_strict("   Resume from that fetch (skip re-fetching)?",
                               default_yes=True):
        review_and_write(payload)
        return True
    runcache.clear()
    utils.info("Discarded the saved fetch; starting fresh.")
    return False


# ---------------------------------------------------------------------------
# Main flow.
# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print(" JOB FINDER v5  —  real jobs, real links, salary-filtered, deduped")
    print("=" * 70)
    print("Everything fetched here is real: genuine IDs and links, each")
    print("validated live before it reaches your Excel files. Sources that")
    print("block automated access are skipped, never faked.")

    loaded = cookies.load_into_session()
    if loaded:
        utils.ok(f"Loaded cookies for: {', '.join(loaded)}")

    # Resume an interrupted run if one is cached (offer before gathering input).
    if offer_resume():
        return

    roles = choose_roles()
    level_codes = choose_experience()
    countries, queries = choose_location()
    selected = choose_sources()

    # Gather source-specific inputs up front, so the core pipeline stays free
    # of console prompts (same core is reused by scheduled / GUI modes).
    career_companies = []
    if "careers" in selected:
        career_companies = ask_companies(
            "Career-page (Greenhouse/Lever/Ashby/SR, or a company's own "
            "careers page)")

    workday_urls = []
    if "workday" in selected:
        print("\n>> Workday career pages")
        for name in ask_companies("Workday"):
            url = menus.ask_text(f"Paste Workday URL for '{name}': ")
            workday_urls.append((name, url))

    settings = {
        "roles": roles,
        "level_codes": level_codes,
        "countries": countries,
        "country": countries[0] if countries else "India",  # back-compat
        "queries": queries,
        "sources": selected,
        "career_companies": career_companies,
        "workday_urls": workday_urls,
    }

    results = core.run_search(settings, ask_url_fn=ask_url_for)

    # Cache the finished fetch BEFORE the QC gate: an accidental "no", an exit,
    # or a dead terminal no longer throws away a multi-minute run — the next
    # launch offers to resume from here.
    saved = runcache.save(results, settings,
                          datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if saved:
        utils.info("Fetch cached — if anything interrupts the review, relaunch "
                   "to resume without re-fetching.")

    review_and_write(results)


# ---------------------------------------------------------------------------
# Headless / scheduled mode (v5).
# ---------------------------------------------------------------------------
def _load_settings_json(path):
    """Read a saved settings file for headless runs. Returns a settings dict."""
    # settingsio is the single source of truth for the on-disk shape + the
    # country/countries fallback (shared with the GUI's "Save settings").
    return settingsio.load(path)


def run_headless(config_path):
    """
    Non-interactive run for scheduling: read saved settings, run the pipeline
    with NO prompts, skip the QC gate, merge only genuinely-new jobs into the
    master tracker, write a "what's new today" digest, and fire a Windows
    toast. Interactive mode is untouched — this is a separate entry path.

    Career-page / Workday sources that need a pasted URL to resolve are still
    honored IF the saved settings already carry the company names / URLs
    (ask_url_fn is None here, so companies that can't auto-detect are skipped
    honestly rather than prompting).
    """
    print("=" * 70)
    print(" JOB FINDER v5  —  headless run (scheduled; no prompts, no QC gate)")
    print("=" * 70)

    loaded = cookies.load_into_session()
    if loaded:
        utils.ok(f"Loaded cookies for: {', '.join(loaded)}")

    try:
        settings = _load_settings_json(config_path)
    except FileNotFoundError:
        utils.warn(
            f"No saved settings at '{config_path}'. Launch the GUI "
            f"(python jobfinder.py --gui), choose your roles / levels / "
            f"countries / cities / sources, and click 'Save settings' — then "
            f"re-run this. The scheduled run uses THOSE selections, not a "
            f"hardcoded default.")
        return 1
    except (OSError, json.JSONDecodeError) as e:
        utils.warn(f"Could not read settings file '{config_path}': {e}")
        return 1

    utils.step(f"Running saved search from '{config_path}'")
    results = core.run_search(settings, ask_url_fn=None)

    total = results.get("total", 0)
    if total == 0:
        utils.warn("No live jobs found for the saved criteria. Nothing merged.")
        notify.notify("Job Finder", "Ran today — no matching jobs found.")
        return 0

    now = datetime.now()
    fetched_at = now.strftime("%Y-%m-%d %H:%M:%S")
    date_stamp = now.strftime("%Y%m%d")

    # Merge first so we know exactly what's genuinely new this run. No user to
    # prompt here — if a file is locked (e.g. the master is open in Excel), fail
    # cleanly, notify, and keep the cache so the next run / an interactive
    # launch can still merge it.
    try:
        added, master_total, new_jobs = tracker.merge(
            {"LinkedIn": results["linkedin"],
             "Career Pages": results["career"],
             "All Sources": results["other"]},
            fetched_at)
        digest_path = excelio.write_whats_new(new_jobs, fetched_at, date_stamp)
    except PermissionError as e:
        locked = getattr(e, "filename", "") or "an output file"
        utils.warn(f"Couldn't write '{locked}' — it's likely open in Excel. "
                   f"Nothing merged; the fetch stays cached for next time.")
        notify.notify("Job Finder",
                      "Run blocked — close master_tracker.xlsx (it's open in "
                      "Excel), then re-run.")
        return 1

    utils.step("Done (headless).")
    utils.ok(f"Master tracker '{config.MASTER_TRACKER}': +{added} new job(s), "
             f"{master_total} total.")
    if digest_path:
        utils.ok(f"What's-new digest: {digest_path}")

    if added:
        notify.notify("Job Finder",
                      f"{added} new job(s) today ({master_total} tracked).")
    else:
        notify.notify("Job Finder",
                      f"Ran today — no NEW jobs ({master_total} already tracked).")
    return 0


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Job Finder — real, validated job postings.")
    parser.add_argument("--headless", action="store_true",
                        help="non-interactive scheduled run (skips the QC gate)")
    parser.add_argument("--config", metavar="settings.json",
                        help="path to saved settings JSON (for --headless)")
    parser.add_argument("--gui", action="store_true",
                        help="launch the graphical interface")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    if args.gui:
        import gui
        gui.launch()
    elif args.headless:
        cfg = args.config or "settings.json"
        sys.exit(run_headless(cfg))
    else:
        try:
            main()
        except KeyboardInterrupt:
            print("\nCancelled. Your fetch (if any) is cached — relaunch to "
                  "resume.")
