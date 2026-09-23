"""
webapp.py — a multi-user browser front-end over the same search pipeline.

This is JUST ANOTHER FRONT-END over core.run_search() (see interface.py): it
collects the same `settings` dict, runs the identical pipeline, and reuses the
existing writers (excelio + tracker). What Phase 3 adds is multi-user support:

  * Accounts + login sessions (accounts.py, SQLite). Open registration.
  * Per-user data isolation (usercontext.py): each user's profile, saved search,
    tracker, output and BYO API keys live in their OWN folder under data/, and
    the pipeline's config paths are redirected to that folder per request.
  * Each user brings their OWN API keys (BYO) — a shared server env var is never
    used for a logged-in user, so no one key leaks across accounts.

Run:
    python webapp.py                 # http://127.0.0.1:8000  (local only)
    python webapp.py --host 0.0.0.0  # LAN / behind a Cloudflare Tunnel

PRIVACY / PII (this is exposed to the internet behind a tunnel):
  * Every data endpoint requires login and operates ONLY in the caller's own
    folder. No endpoint takes a filesystem path; there is no static mount of the
    repo or data dir, and job results are ownership-checked.
  * The uploaded CV is NEVER stored: we extract the (non-identifying) profile
    facts in memory and delete the file immediately. profile.json holds only
    roles/skills/years/education — never name/email/phone.
  * API keys are stored per-user (gitignored) and are NEVER returned to the
    browser (only presence booleans) and NEVER logged.

Concurrency: searches share a process-global narration sink and the per-user
context mutates process-global config, so search/write/meta are serialized with
one lock. Correct for "me + a few friends"; true parallelism is a later step.
"""

import argparse
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import accounts
import config
import core
import cvprofile
import excelio
import llm
import profileio
import runcache
import settingsio
import tracker
import usercontext
import utils

app = FastAPI(title="Job Finder")

_WEB = Path(__file__).with_name("web")
_INDEX = _WEB / "index.html"

SESSION_COOKIE = "jf_session"
_SESSION_MAX_AGE = 60 * 60 * 24 * 30          # 30 days, matches accounts.py
_MAX_CV_BYTES = 12 * 1024 * 1024              # 12 MB upload ceiling

# In-memory search jobs, keyed by job_id and tagged with their owner's user id.
# The per-user context + global narration sink mean one search runs at a time.
_jobs = {}
_jobs_lock = threading.Lock()
_run_lock = threading.Lock()

accounts.init_db()


# ---------------------------------------------------------------------------
# Auth helpers.
# ---------------------------------------------------------------------------
def _current_user(request):
    return accounts.get_session_user(request.cookies.get(SESSION_COOKIE))


def _require_user(request):
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Please log in.")
    return user


def _set_session_cookie(resp, token):
    # httponly + samesite=lax: not readable by JS, not sent on cross-site POSTs.
    # secure is left off so plain http://localhost works; a Cloudflare Tunnel
    # still encrypts the browser<->edge hop.
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                    max_age=_SESSION_MAX_AGE, path="/")


def _run_in_context(user_id, fn):
    """Run fn() with the user's paths/keys/profile active, serialized globally."""
    with _run_lock:
        with usercontext.active_user(user_id):
            return fn()


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
    """Build the core settings dict. Call INSIDE the user's context so the
    role fallback uses that user's profile, not the process default."""
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


# ---------------------------------------------------------------------------
# Background search worker — runs in the owner's context under the global lock.
# ---------------------------------------------------------------------------
def _run_search_bg(job_id, user_id, form):
    job = _jobs[job_id]
    with _run_lock:
        with usercontext.active_user(user_id):
            settings = _normalize_form(form)      # per-user role fallback
            utils.set_output_sink(job["log"].append)
            try:
                results = core.run_search(settings, ask_url_fn=None)
                fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                runcache.save(results, settings, fetched_at)
                job["results"] = results
                job["fetched_at"] = fetched_at
                job["status"] = "done"
            except Exception as e:            # noqa: BLE001 — report, never crash
                job["error"] = str(e)
                job["status"] = "error"
                job["log"].append(f"[!] Search failed: {e}")
            finally:
                utils.reset_output_sink()


# ---------------------------------------------------------------------------
# Auth routes.
# ---------------------------------------------------------------------------
@app.post("/api/register")
async def register(request: Request):
    body = await request.json()
    try:
        user = accounts.create_user(body.get("username", ""), body.get("password", ""))
    except accounts.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    usercontext.ensure_user_dirs(user["id"])
    token = accounts.create_session(user["id"])
    resp = JSONResponse({"user": user})
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    user = accounts.verify_login(body.get("username", ""), body.get("password", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    usercontext.ensure_user_dirs(user["id"])
    token = accounts.create_session(user["id"])
    resp = JSONResponse({"user": user})
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/logout")
async def logout(request: Request):
    accounts.delete_session(request.cookies.get(SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/me")
def me(request: Request):
    return {"user": _require_user(request)}


# ---------------------------------------------------------------------------
# App shell + per-user data routes.
# ---------------------------------------------------------------------------
@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index():
    try:
        return _INDEX.read_text(encoding="utf-8")
    except OSError:
        return HTMLResponse("<h1>Job Finder</h1><p>web/index.html is missing.</p>",
                            status_code=500)


@app.get("/api/meta")
def meta(request: Request):
    """Everything the form needs, computed in the user's own context."""
    user = _require_user(request)

    def build():
        ok, _reason, _labels = llm.available()
        try:
            saved = settingsio.load()
        except (OSError, ValueError):
            saved = None
        p = cvprofile.current_profile()
        return {
            "user": user,
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
            "keys": usercontext.keys_status(user["id"]),
            "saved_settings": _settings_for_form(saved) if saved else None,
        }

    return _run_in_context(user["id"], build)


@app.post("/api/keys")
async def set_keys(request: Request):
    """Save the user's BYO API keys. Returns presence-only status — never the
    values, which are also never logged."""
    user = _require_user(request)
    body = await request.json()
    status = usercontext.save_keys(user["id"], body or {})
    return {"keys": status}


@app.post("/api/upload-cv")
async def upload_cv(request: Request):
    """
    Accept a .docx CV as the raw request body, extract the profile, and DELETE
    the CV immediately (never persisted). Returns the built profile summary.
    """
    user = _require_user(request)
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="No file received.")
    if len(data) > _MAX_CV_BYTES:
        raise HTTPException(status_code=413, detail="CV file is too large (max 12 MB).")
    if data[:2] != b"PK":       # .docx is a zip container; guard the format
        raise HTTPException(status_code=400, detail="Please upload a .docx CV file.")

    def build():
        cv_path = config.MASTER_CV_PATH
        import os
        os.makedirs(os.path.dirname(cv_path) or ".", exist_ok=True)
        try:
            with open(cv_path, "wb") as f:
                f.write(data)
            return profileio.build_and_save()      # reads cv_path, writes profile.json
        finally:
            try:
                os.remove(cv_path)                  # transient: no CV PII on disk
            except OSError:
                pass

    res = _run_in_context(user["id"], build)
    status = 200 if res["ok"] else 400
    return JSONResponse({
        "ok": res["ok"], "error": res["error"], "note": res.get("note", ""),
        "profile": res.get("profile"),
    }, status_code=status)


@app.post("/api/save-settings")
async def save_settings(request: Request):
    user = _require_user(request)
    form = await request.json()

    def save():
        settings = _normalize_form(form)
        try:
            path = settingsio.save(settings)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Couldn't save settings: {e}")
        return {"ok": True, "sources": sorted(settings["sources"]),
                "queries": settings["queries"]}

    return _run_in_context(user["id"], save)


@app.post("/api/search")
async def start_search(request: Request):
    user = _require_user(request)
    form = await request.json()
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "log": [], "results": None,
                         "error": "", "fetched_at": "", "user_id": user["id"]}
    threading.Thread(target=_run_search_bg, args=(job_id, user["id"], form),
                     daemon=True).start()
    return {"job_id": job_id}


def _owned_job(request, job_id):
    """Return the caller's job or 404 (never reveal another user's job)."""
    user = _require_user(request)
    job = _jobs.get(job_id)
    if not job or job.get("user_id") != user["id"]:
        raise HTTPException(status_code=404, detail="unknown job id")
    return user, job


@app.get("/api/search/{job_id}")
def search_status(request: Request, job_id: str):
    _user, job = _owned_job(request, job_id)
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
def write_results(request: Request, job_id: str):
    """Approve: write the per-run workbook and merge into the user's tracker."""
    user, job = _owned_job(request, job_id)
    r = job.get("results")
    if r is None:
        raise HTTPException(status_code=404, detail="no results to write")
    if r.get("total", 0) == 0:
        raise HTTPException(status_code=400, detail="nothing to write")
    fetched_at = job.get("fetched_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def do_write():
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
        return {"path": path, "added": added, "master_total": master_total}

    return _run_in_context(user["id"], do_write)


@app.get("/api/tracker.xlsx")
def download_tracker(request: Request):
    """Stream the caller's own tracker workbook (auth-gated, own folder only)."""
    user = _require_user(request)
    path = Path(usercontext.user_dir(user["id"])) / "master_tracker.xlsx"
    if not path.exists():
        raise HTTPException(status_code=404, detail="No tracker yet — run a search and approve.")
    return FileResponse(
        str(path), filename="master_tracker.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def main():
    parser = argparse.ArgumentParser(description="Job Finder — multi-user web UI.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (use 0.0.0.0 for LAN / a tunnel)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    import uvicorn
    print(f"Job Finder web UI -> http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
