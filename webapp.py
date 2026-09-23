"""
webapp.py — a local browser front-end over the same search pipeline.

This replaces the PowerShell CLI / Tkinter GUI with a web page you open in a
browser. It is JUST ANOTHER FRONT-END over core.run_search() (see interface.py):
it collects the same `settings` dict, runs the identical pipeline on a background
thread, streams the narration, shows the results, and reuses the existing writers
(excelio + tracker) on approval — so output stays identical across front-ends.

Run:
    python webapp.py            # then open http://127.0.0.1:8000
    python webapp.py --host 0.0.0.0 --port 8000   # expose on your LAN / a tunnel

For "me + a few friends" the intended exposure is a Cloudflare Tunnel in front of
this local server (no public cloud). There is NO login yet — that (and per-user
profiles / trackers) is the next phase; today this serves ONE person's data
(the local profile.json + master_tracker.xlsx), like the CLI/GUI.

Secrets are unchanged: API keys are still read at runtime from env vars / the
gitignored secrets files (per user, BYO), never sent to or stored by this app.
"""

import argparse
import threading
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import config
import core
import cvprofile
import excelio
import llm
import profileio
import runcache
import settingsio
import tracker
import utils
from pathlib import Path

app = FastAPI(title="Job Finder")

_INDEX = Path(__file__).with_name("web") / "index.html"

# In-memory search jobs. The narration sink (utils._output_sink) is process-
# global, so we serialize searches with a lock and run one at a time — correct
# for the single-user local use today; per-user isolation comes with accounts.
_jobs = {}
_jobs_lock = threading.Lock()
_run_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Settings: turn the browser form into the core `settings` dict (mirrors the
# CLI's choose_* steps and gui._collect_settings so all front-ends agree).
# ---------------------------------------------------------------------------
def _split_csv(text):
    return [p.strip() for p in str(text or "").split(",") if p.strip()]


def _split_career(text):
    """Company names or careers URLs, comma- or newline-separated."""
    out = []
    for chunk in str(text or "").replace("\r", "\n").split("\n"):
        out.extend(_split_csv(chunk))
    return out


def _parse_workday(text):
    pairs = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        name, url = line.split("|", 1)
        name, url = name.strip(), url.strip()
        if name and url:
            pairs.append((name, url))
    return pairs


def _normalize_form(form):
    roles = _split_csv(form.get("roles")) or list(cvprofile.DEFAULT_ROLES)

    level_codes = [str(c) for c in (form.get("levels") or []) if str(c).strip()]
    if not level_codes:
        level_codes = ["2"]  # default Entry level

    countries = [c for c in (form.get("countries") or []) if str(c).strip()]
    if not countries:
        countries = [config.COUNTRIES[0]]

    cities = _split_csv(form.get("cities"))
    queries = []
    for country in countries:
        if country.lower().startswith("remote"):
            queries.append(country)
        elif cities:
            queries.extend(f"{c}, {country}" for c in cities)
        else:
            queries.append(country)

    sources = {s for s in (form.get("sources") or []) if str(s).strip()}
    if not sources:
        sources = {k for k, (_lbl, en) in config.SOURCES.items() if en}

    return {
        "roles": roles,
        "level_codes": level_codes,
        "countries": countries,
        "country": countries[0],
        "queries": queries,
        "sources": sources,
        "career_companies": _split_career(form.get("career")),
        "workday_urls": _parse_workday(form.get("workday")),
    }


def _flatten(results):
    """One display list across buckets, richest fit first, with a _bucket tag."""
    rows = []
    for bucket in ("linkedin", "career", "other"):
        for j in results.get(bucket, []):
            row = dict(j)
            row["_bucket"] = bucket
            rows.append(row)

    def _key(j):
        fit = j.get("fit_score")
        return fit if isinstance(fit, int) else j.get("match_score", 0)

    rows.sort(key=_key, reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Background search worker.
# ---------------------------------------------------------------------------
def _run_search_bg(job_id, settings):
    job = _jobs[job_id]
    with _run_lock:                      # one search at a time (global sink)
        utils.set_output_sink(job["log"].append)
        try:
            results = core.run_search(settings, ask_url_fn=None)
            fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            runcache.save(results, settings, fetched_at)
            job["results"] = results
            job["fetched_at"] = fetched_at
            job["status"] = "done"
        except Exception as e:           # noqa: BLE001 — report, never crash the server
            job["error"] = str(e)
            job["status"] = "error"
            job["log"].append(f"[!] Search failed: {e}")
        finally:
            utils.reset_output_sink()


# ---------------------------------------------------------------------------
# Routes.
# ---------------------------------------------------------------------------
@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index():
    try:
        return _INDEX.read_text(encoding="utf-8")
    except OSError:
        return HTMLResponse("<h1>Job Finder</h1><p>web/index.html is missing.</p>",
                            status_code=500)


@app.get("/api/meta")
def meta():
    """Everything the form needs: option lists, current profile, LLM + settings."""
    ok, _reason, labels = llm.available()
    try:
        saved = settingsio.load()
    except (OSError, ValueError):
        saved = None
    p = cvprofile.current_profile()
    return {
        "levels": [{"code": code, "label": label}
                   for code, (label, _c) in config.EXPERIENCE_LEVELS.items()],
        "countries": list(config.COUNTRIES),
        "sources": [{"key": k, "label": lbl}
                    for k, (lbl, en) in config.SOURCES.items() if en],
        "profile": {
            "roles": p["roles"], "skills": p["skills"],
            "years_experience": p["years_experience"],
            "has_internship": p["has_internship"],
            "has_bachelors": p["has_bachelors"],
            "has_masters_equiv": p["has_masters_equiv"],
            "has_phd": p["has_phd"],
        },
        "llm": {"ok": ok, "chain": llm.active_label()},
        "saved_settings": _settings_for_form(saved) if saved else None,
    }


def _settings_for_form(s):
    """Project a saved settings dict back onto the browser form fields."""
    return {
        "roles": ", ".join(s.get("roles") or []),
        "levels": list(s.get("level_codes") or []),
        "countries": list(s.get("countries") or ([s["country"]] if s.get("country") else [])),
        "sources": sorted(s.get("sources") or []),
        "career": ", ".join(s.get("career_companies") or []),
        "workday": "\n".join(f"{n} | {u}" for n, u in (s.get("workday_urls") or [])),
    }


@app.post("/api/build-profile")
async def build_profile(request: Request):
    """Parse the master CV into profile.json and activate it (one LLM call)."""
    res = profileio.build_and_save()
    status = 200 if res["ok"] else 400
    return JSONResponse({
        "ok": res["ok"], "error": res["error"], "note": res.get("note", ""),
        "profile": res.get("profile"), "path": res.get("path", ""),
    }, status_code=status)


@app.post("/api/save-settings")
async def save_settings(request: Request):
    form = await request.json()
    settings = _normalize_form(form)
    try:
        path = settingsio.save(settings)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Couldn't save settings: {e}")
    return {"path": path,
            "headless_cmd": "python jobfinder.py --headless",
            "sources": sorted(settings["sources"]),
            "queries": settings["queries"]}


@app.post("/api/search")
async def start_search(request: Request):
    form = await request.json()
    settings = _normalize_form(form)
    import uuid
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "log": [], "results": None,
                         "error": "", "fetched_at": ""}
    threading.Thread(target=_run_search_bg, args=(job_id, settings),
                     daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/search/{job_id}")
def search_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown job id")
    payload = {"status": job["status"], "log": job["log"], "error": job["error"]}
    results = job.get("results")
    if job["status"] == "done" and results is not None:
        payload["summary"] = {
            "total": results.get("total", 0),
            "linkedin": len(results.get("linkedin", [])),
            "career": len(results.get("career", [])),
            "other": len(results.get("other", [])),
            "dropped_salary": results.get("dropped_salary", 0),
            "dropped_senior": results.get("dropped_senior", 0),
            "dropped_duplicate": results.get("dropped_duplicate", 0),
            "dropped_tracked": results.get("dropped_tracked", 0),
            "dropped_ineligible": results.get("dropped_ineligible", 0),
        }
        payload["jobs"] = _flatten(results)
    return payload


@app.post("/api/write/{job_id}")
def write_results(job_id: str):
    """Approve: write the per-run workbook and merge into the master tracker."""
    job = _jobs.get(job_id)
    if not job or job.get("results") is None:
        raise HTTPException(status_code=404, detail="no results to write")
    r = job["results"]
    if r.get("total", 0) == 0:
        raise HTTPException(status_code=400, detail="nothing to write")
    fetched_at = job.get("fetched_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        path = excelio.write_workbook(r["linkedin"], r["career"], r["other"],
                                      fetched_at)
        added, master_total, _new = tracker.merge(
            {"LinkedIn": r["linkedin"], "Career Pages": r["career"],
             "All Sources": r["other"]}, fetched_at)
    except PermissionError as e:
        locked = getattr(e, "filename", "") or "an output file"
        raise HTTPException(
            status_code=409,
            detail=f"Couldn't write '{locked}' — it's open in Excel. Close it "
                   f"and try again (your fetch is safe).")
    runcache.clear()
    return {"path": path, "added": added, "master_total": master_total,
            "master_file": config.MASTER_TRACKER}


def main():
    parser = argparse.ArgumentParser(description="Job Finder — local web UI.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (use 0.0.0.0 for LAN / a tunnel)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    import uvicorn
    print(f"Job Finder web UI -> http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
