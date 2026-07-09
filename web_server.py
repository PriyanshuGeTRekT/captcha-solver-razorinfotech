"""
Backlink Generator Web Server
FastAPI backend + embedded HTML frontend for SEO team backlink automation.
"""
from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
import json
import logging
import os
import random
import re
import socket
import sys
import time
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("captcha_solver").setLevel(logging.DEBUG)
logger = logging.getLogger("backlink_gen")

app = FastAPI(title="Backlink Generator")

# In-memory state
BACKLINK_QUEUE: list[dict] = []
RESULTS: list[dict] = []
LOCK = asyncio.Lock()
PROCESSING = False
WS_CLIENTS: list[WebSocket] = []
SCREENSHOTS_ENABLED = True  # toggled per batch from the submit request
# Bound how many /api/sites/detect calls can each spin up a Chromium at once, so
# the endpoint can't be used to exhaust memory (each detect launches a browser).
DETECT_SEMAPHORE = asyncio.Semaphore(2)
MAX_USER_SITES = 200  # cap the persisted registry so it can't grow without bound


class BacklinkRequest(BaseModel):
    urls: list[str]
    backlink: str
    screenshots: bool = True


class AddSiteRequest(BaseModel):
    url: str
    # Optional manual overrides (advanced). Any left empty are auto-detected at
    # post time from the live page.
    name_field: str = ""
    email_field: str = ""
    url_field: str = ""
    city_field: str = ""
    message_field: str = ""
    captcha_field: str = ""
    submit_selector: str = ""
    success_keywords: list[str] = []
    failure_keywords: list[str] = []


class DetectRequest(BaseModel):
    url: str


# Palette cycled for site dots in the UI (keeps the original 5 colours first).
_SITE_COLORS = ["#7F77DD", "#1D9E75", "#378ADD", "#EF9F27", "#D4537E",
                "#9B59B6", "#16A085", "#E67E22", "#2C82C9", "#C0397B"]


def _validate_public_url(url: str) -> tuple[bool, str]:
    """SSRF guard: only allow http(s) URLs whose host resolves to public IPs.

    Blocks localhost / private / loopback / link-local / reserved ranges so the
    headless browser can't be pointed at internal services — important once the
    server is exposed (cloud deploy binds 0.0.0.0)."""
    # Reject characters that make Python's urlparse and the browser's WHATWG parser
    # disagree on the host (parser-differential SSRF). A backslash is a path
    # separator to Chromium but not to urllib, so "https://10.0.0.1\@public.com/"
    # would validate as public.com yet load 10.0.0.1. '@' (userinfo) and control
    # chars/whitespace are rejected for the same defense-in-depth reason.
    if any(c in (url or "") for c in ("\\", "@")) or any(ord(c) < 0x21 for c in (url or "")):
        return False, "URL contains disallowed characters"
    try:
        p = urlparse(url if "://" in url else "https://" + url)
    except Exception:
        return False, "Invalid URL"
    if p.scheme not in ("http", "https"):
        return False, "URL must start with http:// or https://"
    host = p.hostname
    if not host:
        return False, "URL has no host"
    if host.lower() in ("localhost",) or host.lower().endswith(".local"):
        return False, "Refusing to target a local address"
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False, f"Could not resolve host: {host}"
    for info in infos:
        ip = info[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip.split("%")[0])
        except Exception:
            continue
        if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
                or ip_obj.is_reserved or ip_obj.is_multicast or ip_obj.is_unspecified):
            return False, f"Refusing to target a private/internal address ({ip})"
    return True, ""


async def _attach_ssrf_guard(context) -> None:
    """Abort any NAVIGATION whose host is not public. _validate_public_url only
    checks the initial URL; a 3xx redirect to http://127.0.0.1/ or a cloud-metadata
    IP would otherwise be followed by page.goto. This re-validates every hop."""
    async def _guard(route, request):
        try:
            if request.is_navigation_request():
                ok, _ = await asyncio.to_thread(_validate_public_url, request.url)
                if not ok:
                    logger.warning(f"SSRF guard blocked navigation to {request.url[:80]}")
                    await route.abort()
                    return
        except Exception:
            pass
        try:
            await route.continue_()
        except Exception:
            pass
    try:
        await context.route("**/*", _guard)
    except Exception as e:
        logger.warning(f"Could not attach SSRF guard: {e}")


# Generic validation-error phrases that appear on any site's error banner.
# A visible error means the submission was NOT accepted, so these override any
# success keyword / profile-name match that also happens to be on the page
# (e.g. the form re-rendered with the user's input pre-filled after an error).
GENERIC_ERROR_PHRASES = [
    "the following error", "errors occurred", "error occurred",
    "must be filled", "cannot be blank",
    "muss ausgef", "folgende fehler",
]

RESULTS_FILE = Path("results.json")

# Persistent run history — every batch is recorded so past runs survive a restart
# (and are visible in the History tab). Written INCREMENTALLY as each site finishes,
# so even a crash mid-run leaves a partial record.
RUN_HISTORY_FILE = Path("run_history.json")
RUN_HISTORY: list[dict] = []          # oldest first
CURRENT_RUN_ID: str = ""
MAX_HISTORY = 200


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).hostname.replace("www.", "") if "//" in url else url
    except Exception:
        return url


def _run_counts(results: list[dict]) -> dict:
    c = {"posted": 0, "failed": 0, "unknown": 0, "processing": 0, "queued": 0, "total": len(results)}
    for r in results:
        s = r.get("status", "")
        key = "posted" if s == "success" else s
        if key in c:
            c[key] += 1
    return c


def _load_run_history() -> None:
    global RUN_HISTORY
    try:
        if RUN_HISTORY_FILE.exists():
            RUN_HISTORY = json.loads(RUN_HISTORY_FILE.read_text(encoding="utf-8")) or []
    except Exception:
        RUN_HISTORY = []


def _save_run_history() -> None:
    """Persist history atomically (temp + replace), keeping only the newest runs."""
    global RUN_HISTORY
    try:
        RUN_HISTORY = RUN_HISTORY[-MAX_HISTORY:]
        tmp = RUN_HISTORY_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(RUN_HISTORY), encoding="utf-8")
        tmp.replace(RUN_HISTORY_FILE)
    except Exception:
        pass


def _upsert_current_run(finished: bool = False) -> None:
    """Create/refresh the current run's record from RESULTS and persist it. Called
    after every site so the history reflects progress even if the process dies."""
    if not CURRENT_RUN_ID:
        return
    rec = next((r for r in RUN_HISTORY if r.get("id") == CURRENT_RUN_ID), None)
    if rec is None:
        rec = {"id": CURRENT_RUN_ID, "started_at": time.time(), "finished_at": None,
               "backlink": (RESULTS[0]["backlink"] if RESULTS else ""), "sites": [], "counts": {}}
        RUN_HISTORY.append(rec)
    rec["sites"] = [{
        "url": r["url"], "domain": _domain_of(r["url"]), "status": r["status"],
        "message": r["message"], "captcha_token": r.get("captcha_token", ""),
        "screenshot": r.get("screenshot", ""), "elapsed_ms": r.get("elapsed_ms", 0),
    } for r in RESULTS]
    rec["counts"] = _run_counts(RESULTS)
    if finished:
        rec["finished_at"] = time.time()
    _save_run_history()


def _history_stats() -> dict:
    """Aggregate stats across all recorded runs + per-site success tallies."""
    posted = failed = unknown = 0
    per_site: dict[str, dict] = {}
    for run in RUN_HISTORY:
        for s in run.get("sites", []):
            st = s.get("status", "")
            ps = per_site.setdefault(s.get("domain", "?"), {"posted": 0, "failed": 0, "other": 0})
            if st == "success":
                posted += 1; ps["posted"] += 1
            elif st == "failed":
                failed += 1; ps["failed"] += 1
            elif st in ("unknown",):
                unknown += 1; ps["other"] += 1
    return {"total_runs": len(RUN_HISTORY), "posted": posted, "failed": failed,
            "unknown": unknown, "per_site": per_site}


def _prune_dir(path: Path, keep: int = 300) -> None:
    """Keep only the newest `keep` PNGs in a dir so screenshots/captcha_debug
    don't grow without bound across many runs."""
    try:
        files = sorted(path.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _save_results() -> None:
    """Persist RESULTS so a server restart doesn't lose the last batch's outcome."""
    try:
        RESULTS_FILE.write_text(json.dumps(RESULTS), encoding="utf-8")
    except Exception:
        pass


def _load_results() -> None:
    global RESULTS
    try:
        if RESULTS_FILE.exists():
            RESULTS = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass


# ============================================================
# FRONTEND HTML
# ============================================================

FRONTEND_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RazorInfotech Backlink Generator - Comment section</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #F7F6FC; color: #26215C; min-height: 100vh; -webkit-font-smoothing: antialiased; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 26px 24px 48px; }
.topbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 22px; }
.brand { display: flex; align-items: center; gap: 13px; }
.brand .mark { width: 48px; height: 48px; border-radius: 14px; background: #EEEDFE; color: #534AB7; display: flex; align-items: center; justify-content: center; flex: none; }
.brand h1 { font-size: 20px; font-weight: 700; color: #26215C; letter-spacing: -0.01em; }
.brand p { font-size: 13px; color: #8a8996; font-weight: 400; margin-top: 1px; }
.pill { display: inline-flex; align-items: center; gap: 6px; background: #E1F5EE; color: #0F6E56; padding: 8px 15px; border-radius: 999px; font-size: 13px; font-weight: 600; }
.stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-bottom: 16px; }
.stat { border-radius: 16px; padding: 16px 18px; }
.stat .lbl { display: flex; align-items: center; gap: 7px; font-size: 13px; font-weight: 600; }
.stat .num { font-size: 32px; font-weight: 700; margin-top: 6px; letter-spacing: -0.02em; }
.stat.purple { background: #EEEDFE; } .stat.purple .lbl { color: #534AB7; } .stat.purple .num { color: #26215C; }
.stat.teal { background: #E1F5EE; } .stat.teal .lbl { color: #0F6E56; } .stat.teal .num { color: #04342C; }
.stat.pink { background: #FBEAF0; } .stat.pink .lbl { color: #993556; } .stat.pink .num { color: #4B1528; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; align-items: start; }
.card { background: #fff; border-radius: 18px; padding: 20px; box-shadow: 0 1px 2px rgba(38,33,92,0.05), 0 6px 20px rgba(38,33,92,0.04); }
.card h2 { display: flex; align-items: center; gap: 9px; font-size: 15px; font-weight: 600; color: #26215C; margin-bottom: 16px; }
.card h2 .hi { color: #D85A30; } .card h2 .hg { color: #1D9E75; }
.sublabel { font-size: 12px; color: #8a8996; font-weight: 600; margin-bottom: 8px; display: flex; align-items: center; gap: 6px; }
.sites { display: flex; flex-direction: column; gap: 6px; margin-bottom: 16px; }
.site { display: flex; align-items: center; gap: 10px; padding: 9px 12px; border-radius: 11px; background: #FAFAFC; transition: background 0.15s, opacity 0.15s; cursor: pointer; }
.site:hover { background: #F4F2FD; }
.site .dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
.site .nm { font-size: 12.5px; color: #3a3947; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.site.off { opacity: 0.45; }
.site.off .nm { text-decoration: line-through; }
.site .nm { flex: 1; }
.site-right { display: flex; align-items: center; gap: 8px; flex: none; }
.site .rm { border: none; background: none; color: #b7b6c4; cursor: pointer; font-size: 16px; line-height: 1; padding: 2px 4px; border-radius: 6px; flex: none; }
.site .rm:hover { background: #FBEAF0; color: #993556; }
.site .tag { font-size: 9.5px; font-weight: 700; letter-spacing: 0.03em; color: #8079c9; background: #EEEDFE; padding: 2px 6px; border-radius: 6px; text-transform: uppercase; flex: none; }
.addsite { display: flex; gap: 8px; margin-bottom: 8px; }
.addsite input { flex: 1; border: 1.5px solid #CECBF6; border-radius: 11px; padding: 9px 12px; background: #fff; font-size: 12.5px; color: #26215C; font-family: inherit; outline: none; transition: border-color 0.15s, box-shadow 0.15s; }
.addsite input:focus { border-color: #7F77DD; box-shadow: 0 0 0 3px rgba(127,119,221,0.15); }
.addsite input::placeholder { color: #b3b2c0; }
.btn-mini { border: none; border-radius: 11px; padding: 9px 14px; font-size: 12.5px; font-weight: 600; cursor: pointer; font-family: inherit; background: #F1EFE8; color: #5F5E5A; transition: filter 0.15s, background 0.15s; flex: none; }
.btn-mini:hover { background: #E7E4DA; }
.btn-mini.primary { background: #534AB7; color: #fff; }
.btn-mini.primary:hover { filter: brightness(1.08); }
.btn-mini:disabled { opacity: 0.55; cursor: not-allowed; }
.addsite-msg { font-size: 11.5px; margin-bottom: 14px; min-height: 15px; }
.addsite-msg.err { color: #993556; }
.addsite-msg.ok { color: #0F6E56; }
.addsite-msg.info { color: #8a8996; }
.detect-panel { background: #fff; border-radius: 18px; padding: 22px; max-width: 680px; width: 100%; max-height: 88vh; overflow-y: auto; }
.detect-panel h3 { font-size: 16px; font-weight: 700; color: #26215C; margin-bottom: 4px; }
.detect-panel .sub { font-size: 12px; color: #8a8996; margin-bottom: 14px; word-break: break-all; }
.detect-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 14px; }
.detect-fields .fld { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 10px; background: #FAFAFC; font-size: 12px; }
.detect-fields .fld .k { color: #8a8996; width: 74px; flex: none; }
.detect-fields .fld .v { font-family: ui-monospace, Menlo, Consolas, monospace; color: #26215C; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.detect-fields .fld.miss .v { color: #c39; font-style: italic; }
.detect-warn { font-size: 11.5px; color: #854F0B; background: #FAEEDA; border-radius: 9px; padding: 8px 11px; margin-bottom: 12px; line-height: 1.5; }
.detect-shot { width: 100%; border-radius: 10px; border: 1px solid #EDEBF6; margin-bottom: 14px; }
.detect-actions { display: flex; gap: 10px; }
.tabs { display: inline-flex; gap: 4px; background: #EEEDFE; padding: 4px; border-radius: 12px; margin-bottom: 16px; }
.tab { border: none; background: transparent; color: #6b6a86; font-size: 13.5px; font-weight: 600; padding: 8px 18px; border-radius: 9px; cursor: pointer; font-family: inherit; transition: background 0.15s, color 0.15s; }
.tab.active { background: #fff; color: #26215C; box-shadow: 0 1px 2px rgba(38,33,92,0.08); }
.hist-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 18px; }
.hist-stat { background: #FAFAFC; border-radius: 12px; padding: 14px 16px; }
.hist-stat .n { font-size: 26px; font-weight: 700; letter-spacing: -0.02em; }
.hist-stat .l { font-size: 12px; color: #8a8996; font-weight: 600; margin-top: 2px; }
.hist-stat.g .n { color: #0F6E56; } .hist-stat.r .n { color: #993556; } .hist-stat.p .n { color: #534AB7; }
.persite { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 18px; }
.persite .chip { display: flex; align-items: center; gap: 7px; background: #FAFAFC; border-radius: 999px; padding: 5px 12px; font-size: 12px; color: #3a3947; }
.persite .chip b { color: #0F6E56; } .persite .chip .x { color: #993556; }
.run { border: 1px solid #EDEBF6; border-radius: 14px; margin-bottom: 10px; overflow: hidden; }
.run-head { display: flex; align-items: center; gap: 10px; padding: 13px 15px; cursor: pointer; }
.run-head:hover { background: #FAFAFC; }
.run-head .when { font-size: 12.5px; font-weight: 600; color: #26215C; }
.run-head .bl { font-size: 11.5px; color: #8a8996; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.run-head .cnt { display: flex; gap: 5px; flex: none; }
.run-head .chev { color: #b7b6c4; flex: none; transition: transform 0.15s; }
.run.open .chev { transform: rotate(90deg); }
.run-body { display: none; padding: 4px 15px 13px; border-top: 1px solid #F1EFF9; }
.run.open .run-body { display: block; }
.run-site { display: flex; align-items: center; gap: 9px; padding: 7px 0; font-size: 12.5px; border-bottom: 1px dashed #F1EFF9; }
.run-site:last-child { border-bottom: none; }
.run-site .rs-dom { flex: 1; color: #3a3947; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.run-site .rs-msg { color: #9b9aa6; font-size: 11px; }
.hist-empty { text-align: center; color: #9b9aa6; padding: 40px 16px; font-size: 13.5px; }
.hist-actions { display: flex; justify-content: flex-end; margin-top: 8px; }
.switch { position: relative; display: inline-flex; width: 38px; height: 22px; margin-left: auto; flex: none; }
.switch input { position: absolute; opacity: 0; width: 0; height: 0; }
.switch .slider { position: absolute; inset: 0; background: #CFCDda; border-radius: 999px; transition: background 0.2s; }
.switch .slider::before { content: ""; position: absolute; width: 16px; height: 16px; left: 3px; top: 3px; background: #fff; border-radius: 50%; transition: transform 0.2s; box-shadow: 0 1px 2px rgba(38,33,92,0.25); }
.switch input:checked + .slider { background: #534AB7; }
.switch input:checked + .slider::before { transform: translateX(16px); }
.opt-row { display: flex; align-items: center; gap: 11px; padding: 11px 12px; border-radius: 12px; background: #FAFAFC; margin-bottom: 16px; cursor: pointer; }
.opt-ic { width: 32px; height: 32px; border-radius: 9px; background: #EEEDFE; color: #534AB7; display: flex; align-items: center; justify-content: center; flex: none; }
.opt-txt { display: flex; flex-direction: column; gap: 1px; }
.opt-t { font-size: 13px; font-weight: 500; color: #26215C; }
.opt-d { font-size: 11.5px; color: #8a8996; }
.field { display: flex; align-items: center; gap: 9px; border: 1.5px solid #CECBF6; border-radius: 12px; padding: 11px 13px; margin-bottom: 16px; background: #fff; transition: border-color 0.15s, box-shadow 0.15s; }
.field:focus-within { border-color: #7F77DD; box-shadow: 0 0 0 3px rgba(127,119,221,0.15); }
.field .fi { color: #7F77DD; flex: none; display: flex; }
.field input { border: none; outline: none; background: transparent; font-size: 13.5px; color: #26215C; width: 100%; font-family: inherit; }
.field input::placeholder { color: #b3b2c0; }
.bl-input { width: 100%; border: 1.5px solid #CECBF6; border-radius: 12px; padding: 11px 13px; margin-bottom: 6px; background: #fff; font-size: 13.5px; line-height: 1.5; color: #26215C; font-family: inherit; resize: vertical; min-height: 46px; transition: border-color 0.15s, box-shadow 0.15s; }
.bl-input:focus { outline: none; border-color: #7F77DD; box-shadow: 0 0 0 3px rgba(127,119,221,0.15); }
.bl-input::placeholder { color: #b3b2c0; }
.bl-hint { font-size: 11.5px; color: #9b9aa6; margin-bottom: 16px; }
.res-bl { font-size: 11px; color: #8a8996; margin: -2px 0 8px; display: flex; align-items: center; gap: 5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.res-bl .a { color: #7F77DD; }
.actions { display: flex; gap: 10px; }
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; border: none; border-radius: 13px; padding: 12px 16px; font-size: 14px; font-weight: 600; cursor: pointer; font-family: inherit; transition: filter 0.15s, transform 0.1s, background 0.15s; }
.btn:active { transform: scale(0.985); }
.btn-primary { flex: 1; background: #534AB7; color: #fff; }
.btn-primary:hover { filter: brightness(1.08); }
.btn-primary:disabled { opacity: 0.55; cursor: not-allowed; filter: none; }
.btn-ghost { background: #F1EFE8; color: #5F5E5A; }
.btn-ghost:hover { background: #E7E4DA; }
.progress { height: 8px; border-radius: 999px; background: #EEEDFE; margin-top: 18px; overflow: hidden; }
.progress .fill { height: 100%; background: #1D9E75; border-radius: 999px; width: 0%; transition: width 0.35s ease; }
.progress-label { font-size: 11.5px; color: #8a8996; margin-top: 7px; text-align: right; }
.empty { text-align: center; padding: 46px 16px; color: #b0aec2; }
.empty .ei { color: #CECBF6; margin-bottom: 12px; display: inline-flex; }
.empty p { font-size: 13.5px; color: #9b9aa6; max-width: 240px; margin: 0 auto; line-height: 1.5; }
.res { border: 1px solid #EDEBF6; border-radius: 14px; padding: 14px; margin-bottom: 10px; }
.res:last-child { margin-bottom: 0; }
.res-head { display: flex; align-items: center; gap: 8px; margin-bottom: 7px; }
.res-head .dom { font-size: 12.5px; font-weight: 600; color: #26215C; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.badge { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; font-weight: 600; padding: 3px 9px; border-radius: 999px; flex: none; }
.badge.ok { background: #E1F5EE; color: #0F6E56; }
.badge.fail { background: #FBEAF0; color: #993556; }
.badge.work { background: #FAEEDA; color: #854F0B; }
.badge.queue { background: #F1EFE8; color: #5F5E5A; }
.res-msg { font-size: 12px; color: #6b6a78; margin-bottom: 9px; line-height: 1.45; }
.captcha { display: flex; align-items: center; gap: 8px; margin-bottom: 9px; }
.captcha .k { font-size: 11px; color: #8a8996; }
.captcha .v { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; color: #534AB7; background: #EEEDFE; padding: 2px 9px; border-radius: 7px; }
.thumbs { display: flex; gap: 8px; }
.thumb { flex: 1; }
.thumb .t { font-size: 10.5px; color: #9b9aa6; margin-bottom: 4px; }
.thumb img { width: 100%; height: auto; max-height: 150px; object-fit: cover; border-radius: 10px; border: 1px solid #EDEBF6; cursor: pointer; display: block; }
.spin { animation: spin 0.9s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.pulse { animation: pulse 1.4s ease-in-out infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
.modal { display: none; position: fixed; inset: 0; background: rgba(38,33,92,0.6); z-index: 1000; justify-content: center; align-items: center; padding: 28px; }
.modal.active { display: flex; }
.modal img { max-width: 92%; max-height: 92%; border-radius: 14px; box-shadow: 0 20px 60px rgba(0,0,0,0.4); }
@media (max-width: 860px) { .grid { grid-template-columns: 1fr; } .stats { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="wrap">
    <div class="topbar">
        <div class="brand">
            <div class="mark"><svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 15l6-6"/><path d="M11 6l1-1a4 4 0 0 1 6 6l-2 2"/><path d="M13 18l-1 1a4 4 0 0 1-6-6l2-2"/></svg></div>
            <div>
                <h1>RazorInfotech Backlink Generator</h1>
                <p>Comment section &middot; automated SEO backlinks</p>
            </div>
        </div>
        <div class="pill"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg> Ready</div>
    </div>

    <div class="stats">
        <div class="stat purple">
            <div class="lbl"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18"/></svg> Total sites</div>
            <div class="num" id="stat-total">0</div>
        </div>
        <div class="stat teal">
            <div class="lbl"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-5"/></svg> Posted</div>
            <div class="num" id="stat-success">0</div>
        </div>
        <div class="stat pink">
            <div class="lbl"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 16H3z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg> Failed</div>
            <div class="num" id="stat-failed">0</div>
        </div>
    </div>

    <div class="tabs">
        <button class="tab active" id="tab-run" onclick="switchTab('run')">New run</button>
        <button class="tab" id="tab-history" onclick="switchTab('history')">History</button>
    </div>

    <div id="view-run">
    <div class="grid">
        <div class="card">
            <h2><span class="hi"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4z"/></svg></span> Submit backlinks</h2>
            <div class="sublabel"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h16M4 18h16"/></svg> Target sites &middot; toggle to include</div>
            <div class="sites" id="sites-list"></div>
            <div class="addsite">
                <input id="new-site-url" type="text" placeholder="Add a site by URL — e.g. https://example.com/guestbook" onkeydown="if(event.key==='Enter'){event.preventDefault();addSite();}">
                <button class="btn-mini" id="btn-detect" onclick="detectSite()" title="Preview the fields we'll auto-detect">Detect</button>
                <button class="btn-mini primary" id="btn-add" onclick="addSite()">Add</button>
            </div>
            <div class="addsite-msg info" id="addsite-msg">Paste a guestbook or comment-form URL. We auto-detect the fields &amp; captcha.</div>
            <div class="sublabel">Your backlink URL(s)</div>
            <textarea id="backlink" class="bl-input" rows="3" placeholder="https://temp.com&#10;https://another-client-site.com"></textarea>
            <div class="bl-hint">Add one URL per line &mdash; all your URLs are posted together in a single comment on each site above.</div>
            <label class="opt-row">
                <span class="opt-ic"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h3l1.5-2h7L17 7h3v12H4z"/><circle cx="12" cy="13" r="3.2"/></svg></span>
                <div class="opt-txt"><span class="opt-t">Capture screenshots</span><span class="opt-d">Save before &amp; after images of each submission</span></div>
                <span class="switch"><input type="checkbox" id="opt-screenshots" checked><span class="slider"></span></span>
            </label>
            <div class="actions">
                <button class="btn btn-primary" id="btn-submit" onclick="submitBacklinks()"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4z"/></svg> Start generating</button>
                <button class="btn btn-ghost" onclick="clearAll()"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18M6 6l12 12"/></svg> Clear</button>
            </div>
            <div class="progress" id="progress-bar" style="display:none">
                <div class="fill" id="progress-fill" style="width:0%"></div>
            </div>
            <div class="progress-label" id="progress-label" style="display:none"></div>
        </div>

        <div class="card">
            <h2><span class="hg"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h10M4 12h10M4 18h7"/><path d="M16 15l2 2 4-4"/></svg></span> Results</h2>
            <div id="results-container">
                <div class="empty">
                    <span class="ei"><svg viewBox="0 0 24 24" width="40" height="40" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 15l6-6"/><path d="M11 6l1-1a4 4 0 0 1 6 6l-2 2"/><path d="M13 18l-1 1a4 4 0 0 1-6-6l2-2"/></svg></span>
                    <p>Enter your backlink URL and press start to see results here.</p>
                </div>
            </div>
        </div>
    </div>
    </div>

    <div id="view-history" style="display:none">
        <div class="card">
            <h2><span class="hg"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 14l3-3 3 3 5-6"/></svg></span> Run history</h2>
            <div class="hist-stats" id="hist-stats"></div>
            <div class="persite" id="hist-persite"></div>
            <div id="history-list"></div>
            <div class="hist-actions"><button class="btn-mini" onclick="clearHistory()">Clear history</button></div>
        </div>
    </div>
</div>

<div class="modal" id="screenshot-modal" onclick="this.classList.remove('active')">
    <img id="screenshot-img" src="">
</div>

<div class="modal" id="detect-modal">
    <div class="detect-panel">
        <h3>Detected form fields</h3>
        <div class="sub" id="detect-url"></div>
        <div class="detect-fields" id="detect-fields"></div>
        <div id="detect-warnings"></div>
        <img class="detect-shot" id="detect-shot" src="" alt="page preview" style="display:none">
        <div class="detect-actions">
            <button class="btn btn-primary" id="btn-confirm-add" onclick="confirmAddDetected()">Add this site</button>
            <button class="btn btn-ghost" onclick="closeDetect()">Cancel</button>
        </div>
    </div>
</div>

<script>
let ws = null;
let reconnectTimer = null;

const IC = {
  send: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4z"/></svg>',
  loader: '<svg class="spin" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a9 9 0 1 0 9 9"/></svg>',
  check: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
  alert: '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 16H3z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
  chev: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>'
};
const EMPTY_HTML = '<div class="empty"><span class="ei"><svg viewBox="0 0 24 24" width="40" height="40" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 15l6-6"/><path d="M11 6l1-1a4 4 0 0 1 6 6l-2 2"/><path d="M13 18l-1 1a4 4 0 0 1-6-6l2-2"/></svg></span><p>Enter your backlink URL and press start to see results here.</p></div>';
function domainOf(u) { try { return new URL(u).hostname.replace(/^www\\./, ''); } catch (e) { return u; } }

function connectWebSocket() {
    if (ws && ws.readyState === WebSocket.OPEN) return;
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(protocol + '//' + location.host + '/ws');

    ws.onopen = () => {
        console.log('WebSocket connected');
        if (reconnectTimer) { clearInterval(reconnectTimer); reconnectTimer = null; }
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'state') {
            renderResults(data.results);
            updateStats(data.results);
            updateProgress(data.progress);
            if (!data.processing) {
                document.getElementById('progress-bar').style.display = 'none';
                document.getElementById('progress-label').style.display = 'none';
                const b = document.getElementById('btn-submit');
                b.disabled = false;
                b.innerHTML = IC.send + ' Start generating';
            }
            // Keep the History tab live if it's the one being viewed.
            if (historyVisible()) loadHistory();
        }
    };

    ws.onclose = () => {
        console.log('WebSocket disconnected, reconnecting...');
        if (!reconnectTimer) reconnectTimer = setInterval(connectWebSocket, 3000);
    };

    ws.onerror = () => ws.close();
}

function onSiteToggle(cb) {
    const row = cb.closest('.site');
    if (row) row.classList.toggle('off', !cb.checked);
}

function selectedUrls() {
    return Array.from(document.querySelectorAll('.site-cb'))
        .filter(cb => cb.checked)
        .map(cb => cb.dataset.url);
}

// ---- Dynamic site list (built-in + user-added) ----
function setMsg(text, cls) {
    const el = document.getElementById('addsite-msg');
    el.className = 'addsite-msg ' + (cls || 'info');
    el.innerHTML = text;
}

function siteRow(s, checked) {
    const rm = s.builtin ? '' :
        `<button class="rm" type="button" title="Remove site" data-domain="${esc(s.domain)}">&times;</button>`;
    const tag = s.builtin ? '' : '<span class="tag">added</span>';
    return `<label class="site${checked ? '' : ' off'}">`
        + `<span class="dot" style="background:${esc(s.color)}"></span>`
        + `<span class="nm" title="${esc(s.url)}">${esc(s.domain)}</span>`
        + `<span class="site-right">${tag}${rm}`
        + `<span class="switch"><input type="checkbox" class="site-cb" data-url="${esc(s.url)}" ${checked ? 'checked' : ''} onchange="onSiteToggle(this)"><span class="slider"></span></span>`
        + `</span></label>`;
}

async function loadSites() {
    const list = document.getElementById('sites-list');
    // Delegated remove handler (attached once): reads the domain from data-domain,
    // so a domain value can never break out of an inline onclick string.
    if (!list._rmBound) {
        list.addEventListener('click', (e) => {
            const btn = e.target.closest('.rm');
            if (btn) { e.preventDefault(); e.stopPropagation(); removeSite(btn.dataset.domain); }
        });
        list._rmBound = true;
    }
    // Preserve which sites the user had switched OFF across a reload.
    const prevOff = new Set(Array.from(document.querySelectorAll('.site-cb')).filter(cb => !cb.checked).map(cb => cb.dataset.url));
    const hadRows = document.querySelectorAll('.site-cb').length > 0;
    try {
        const r = await fetch('/api/sites');
        const data = await r.json();
        list.innerHTML = (data.sites || []).map(s => siteRow(s, hadRows ? !prevOff.has(s.url) : true)).join('');
    } catch (e) {
        list.innerHTML = '<div class="addsite-msg err">Could not load the site list.</div>';
    }
}

async function addSite() {
    const inp = document.getElementById('new-site-url');
    const url = inp.value.trim();
    if (!url) { setMsg('Enter a site URL first.', 'err'); return; }
    const btn = document.getElementById('btn-add');
    btn.disabled = true; setMsg('Adding…', 'info');
    try {
        const r = await fetch('/api/sites', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) });
        const data = await r.json();
        if (!r.ok) { setMsg(esc(data.detail || 'Could not add site.'), 'err'); }
        else { inp.value = ''; await loadSites(); setMsg('Added <b>' + esc(data.domain) + '</b> — fields are auto-detected when you post.', 'ok'); }
    } catch (e) { setMsg('Network error adding site.', 'err'); }
    btn.disabled = false;
}

let _detectUrl = '';
async function detectSite() {
    const inp = document.getElementById('new-site-url');
    const url = inp.value.trim();
    if (!url) { setMsg('Enter a site URL to detect.', 'err'); return; }
    const btn = document.getElementById('btn-detect');
    btn.disabled = true; setMsg('Loading the page &amp; detecting fields… this can take ~15s.', 'info');
    try {
        const r = await fetch('/api/sites/detect', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) });
        const data = await r.json();
        if (!r.ok) { setMsg(esc(data.detail || 'Detection failed.'), 'err'); }
        else { _detectUrl = url; showDetect(data); setMsg('', 'info'); }
    } catch (e) { setMsg('Network error during detection.', 'err'); }
    btn.disabled = false;
}

function showDetect(d) {
    document.getElementById('detect-url').textContent = d.url || '';
    const labels = { name: 'Name', email: 'Email', url: 'Website', city: 'City', message: 'Message', captcha: 'Captcha', captcha_hash: 'Captcha hash', submit: 'Submit btn' };
    const f = d.fields || {};
    document.getElementById('detect-fields').innerHTML = Object.keys(labels).map(k => {
        const v = f[k];
        const miss = !v;
        const shown = v ? esc(v) : ((k === 'captcha' || k === 'captcha_hash') ? '— none —' : 'not found');
        return `<div class="fld${miss ? ' miss' : ''}"><span class="k">${labels[k]}</span><span class="v">${shown}</span></div>`;
    }).join('');
    const warn = document.getElementById('detect-warnings');
    warn.innerHTML = (d.warnings && d.warnings.length) ? '<div class="detect-warn">' + d.warnings.map(esc).join('<br>') + '</div>' : '';
    const shot = document.getElementById('detect-shot');
    if (d.screenshot) { shot.src = d.screenshot; shot.style.display = 'block'; } else { shot.style.display = 'none'; }
    document.getElementById('detect-modal').classList.add('active');
}

function closeDetect() { document.getElementById('detect-modal').classList.remove('active'); }

async function confirmAddDetected() {
    closeDetect();
    if (_detectUrl) document.getElementById('new-site-url').value = _detectUrl;
    await addSite();
}

async function removeSite(domain) {
    if (!confirm('Remove ' + domain + ' from the list?')) return;
    try {
        const r = await fetch('/api/sites/' + encodeURIComponent(domain), { method: 'DELETE' });
        if (r.ok) { await loadSites(); setMsg('Removed ' + esc(domain) + '.', 'ok'); }
        else { const d = await r.json(); setMsg(esc(d.detail || 'Could not remove site.'), 'err'); }
    } catch (e) { setMsg('Network error removing site.', 'err'); }
}

// ---- History tab ----
function switchTab(which) {
    const isRun = which === 'run';
    document.getElementById('view-run').style.display = isRun ? '' : 'none';
    document.getElementById('view-history').style.display = isRun ? 'none' : '';
    document.getElementById('tab-run').classList.toggle('active', isRun);
    document.getElementById('tab-history').classList.toggle('active', !isRun);
    if (!isRun) loadHistory();
}
function historyVisible() {
    const v = document.getElementById('view-history');
    return v && v.style.display !== 'none';
}
function fmtTime(epoch) {
    if (!epoch) return '';
    const d = new Date(epoch * 1000);
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + ' min ago';
    if (diff < 86400) return Math.floor(diff / 3600) + ' hr ago';
    return d.toLocaleString();
}
function cntBadges(c) {
    c = c || {};
    const p = [];
    if (c.posted) p.push('<span class="badge ok">' + IC.check + ' ' + c.posted + '</span>');
    if (c.failed) p.push('<span class="badge fail">' + IC.alert + ' ' + c.failed + '</span>');
    if (c.unknown) p.push('<span class="badge queue">? ' + c.unknown + '</span>');
    if ((c.processing || 0) + (c.queued || 0)) p.push('<span class="badge work">' + IC.loader + ' ' + ((c.processing || 0) + (c.queued || 0)) + '</span>');
    return p.join(' ');
}
function siteBadge(status) {
    if (status === 'success') return '<span class="badge ok">' + IC.check + ' Posted</span>';
    if (status === 'failed') return '<span class="badge fail">' + IC.alert + ' Failed</span>';
    if (status === 'processing') return '<span class="badge work">' + IC.loader + ' Working</span>';
    if (status === 'unknown') return '<span class="badge queue">Unknown</span>';
    return '<span class="badge queue">Queued</span>';
}
async function loadHistory() {
    try {
        const r = await fetch('/api/history');
        renderHistory(await r.json());
    } catch (e) {
        document.getElementById('history-list').innerHTML = '<div class="hist-empty">Could not load history.</div>';
    }
}
function renderHistory(d) {
    const s = d.stats || {};
    document.getElementById('hist-stats').innerHTML =
        '<div class="hist-stat p"><div class="n">' + (s.total_runs || 0) + '</div><div class="l">Total runs</div></div>' +
        '<div class="hist-stat g"><div class="n">' + (s.posted || 0) + '</div><div class="l">Backlinks posted</div></div>' +
        '<div class="hist-stat r"><div class="n">' + (s.failed || 0) + '</div><div class="l">Failed attempts</div></div>';
    const ps = s.per_site || {};
    document.getElementById('hist-persite').innerHTML = Object.keys(ps).sort().map(dom => {
        const v = ps[dom];
        return '<div class="chip">' + esc(dom) + ' <b>' + v.posted + '✓</b>' + (v.failed ? ' <span class="x">' + v.failed + '✗</span>' : '') + '</div>';
    }).join('');
    const runs = d.runs || [];
    if (!runs.length) {
        document.getElementById('history-list').innerHTML = '<div class="hist-empty">No runs yet — start a run and it will show up here.</div>';
        return;
    }
    document.getElementById('history-list').innerHTML = runs.map((run, i) => {
        const sites = (run.sites || []).map(st =>
            '<div class="run-site">' + siteBadge(st.status) + '<span class="rs-dom">' + esc(st.domain) + '</span><span class="rs-msg">' + esc(st.message || '') + '</span></div>'
        ).join('');
        const running = !run.finished_at;
        return '<div class="run' + (i === 0 ? ' open' : '') + '">'
            + '<div class="run-head" onclick="toggleRun(this)">'
            + '<span class="chev">' + IC.chev + '</span>'
            + '<span class="when">' + (running ? 'Running… ' : '') + fmtTime(run.started_at) + '</span>'
            + '<span class="bl">' + esc(run.backlink || '') + '</span>'
            + '<span class="cnt">' + cntBadges(run.counts) + '</span>'
            + '</div><div class="run-body">' + sites + '</div></div>';
    }).join('');
}
async function clearHistory() {
    if (!confirm('Clear all run history?')) return;
    try { await fetch('/api/history', { method: 'DELETE' }); loadHistory(); } catch (e) {}
}
function toggleRun(el) { el.parentElement.classList.toggle('open'); }

async function submitBacklinks() {
    const urls = selectedUrls();
    const backlink = document.getElementById('backlink').value.trim();
    const screenshots = document.getElementById('opt-screenshots').checked;
    if (!urls.length) { alert('Please enable at least one target site.'); return; }
    if (!backlink) { alert('Please enter at least one backlink URL.'); return; }

    const btn = document.getElementById('btn-submit');
    btn.disabled = true;
    btn.innerHTML = IC.loader + ' Processing…';
    document.getElementById('progress-bar').style.display = 'block';
    document.getElementById('progress-label').style.display = 'block';
    connectWebSocket();

    await fetch('/api/submit', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({urls, backlink, screenshots})
    });
}

function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function renderResults(results) {
    const container = document.getElementById('results-container');
    if (results.length === 0) { container.innerHTML = EMPTY_HTML; return; }
    container.innerHTML = results.map(r => {
        let badge;
        if (r.status === 'success') badge = '<span class="badge ok">' + IC.check + ' Posted</span>';
        else if (r.status === 'failed') badge = '<span class="badge fail">' + IC.alert + ' Failed</span>';
        else if (r.status === 'processing') badge = '<span class="badge work">' + IC.loader + ' Working</span>';
        else badge = '<span class="badge queue">Queued</span>';
        const bl = r.backlink ? `<div class="res-bl"><span class="a">&#8627;</span> ${esc(r.backlink)}</div>` : '';
        const captcha = r.captcha_token ? `<div class="captcha"><span class="k">Captcha</span><span class="v">${esc(r.captcha_token)}</span></div>` : '';
        let thumbs = '';
        if (r.screenshot) {
            const before = r.screenshot_before || r.screenshot;
            thumbs = `<div class="thumbs"><div class="thumb"><div class="t">Before submit</div><img src="${before}" loading="lazy" onclick="showScreenshot('${before}')"></div><div class="thumb"><div class="t">After submit</div><img src="${r.screenshot}" loading="lazy" onclick="showScreenshot('${r.screenshot}')"></div></div>`;
        }
        return `<div class="res"><div class="res-head">${badge}<span class="dom">${esc(domainOf(r.url))}</span></div>${bl}<div class="res-msg">${esc(r.message || '')}</div>${captcha}${thumbs}</div>`;
    }).reverse().join('');
}

function updateStats(results) {
    const total = results.length;
    const success = results.filter(r => r.status === 'success').length;
    const failed = results.filter(r => r.status === 'failed').length;
    document.getElementById('stat-total').textContent = total;
    document.getElementById('stat-success').textContent = success;
    document.getElementById('stat-failed').textContent = failed;
    const done = success + failed;
    if (total > 0) {
        document.getElementById('progress-fill').style.width = Math.round((done / total) * 100) + '%';
        document.getElementById('progress-label').textContent = done + ' of ' + total + ' complete';
    }
}

function updateProgress(progress) {
    // Progress bar and label are derived from results in updateStats().
}

function showScreenshot(src) {
    document.getElementById('screenshot-img').src = src;
    document.getElementById('screenshot-modal').classList.add('active');
}

function clearAll() {
    document.getElementById('backlink').value = '';
    document.querySelectorAll('.site-cb').forEach(cb => {
        cb.checked = true;
        const row = cb.closest('.site');
        if (row) row.classList.remove('off');
    });
    document.getElementById('opt-screenshots').checked = true;
    document.getElementById('results-container').innerHTML = EMPTY_HTML;
    document.getElementById('stat-total').textContent = '0';
    document.getElementById('stat-success').textContent = '0';
    document.getElementById('stat-failed').textContent = '0';
    document.getElementById('progress-bar').style.display = 'none';
    document.getElementById('progress-label').style.display = 'none';
    document.getElementById('progress-fill').style.width = '0%';
}

// Initial load
loadSites();
connectWebSocket();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return FRONTEND_HTML


@app.get("/api/results")
async def get_results():
    from src.site_configs import get_all_configs
    total_configured = len(get_all_configs())
    done = len([r for r in RESULTS if r["status"] != "processing"])
    return {
        "results": RESULTS,
        "progress": {"done": done, "total": max(done, 1)},
        "configs_available": total_configured,
    }


@app.get("/api/history")
async def get_history():
    """Past runs (newest first) + aggregate stats for the History tab."""
    return {"runs": list(reversed(RUN_HISTORY)), "stats": _history_stats()}


@app.delete("/api/history")
async def clear_history():
    global RUN_HISTORY
    RUN_HISTORY = []
    _save_run_history()
    return {"status": "ok"}


@app.get("/api/sites")
async def list_sites():
    """Merged list of built-in + user-added target sites for the UI."""
    from src.site_configs import get_all_configs, SITE_CONFIGS, normalize_domain
    builtin_norm = {normalize_domain(d) for d in SITE_CONFIGS}
    sites = []
    for i, (domain, cfg) in enumerate(get_all_configs().items()):
        sites.append({
            "domain": domain,
            "url": cfg.url_template or ("https://" + domain),
            "label": domain,
            "color": _SITE_COLORS[i % len(_SITE_COLORS)],
            "builtin": domain in builtin_norm,
        })
    return {"sites": sites}


@app.post("/api/sites")
async def add_site(req: AddSiteRequest):
    """Register (and persist) a new target site by URL, with optional overrides."""
    from src.site_configs import add_user_site, SiteFormConfig, normalize_domain, SITE_CONFIGS
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(400, "URL is required")
    if "://" not in url:
        url = "https://" + url
    ok, msg = await asyncio.to_thread(_validate_public_url, url)
    if not ok:
        raise HTTPException(400, msg)
    domain = normalize_domain(url)
    if not domain:
        raise HTTPException(400, "Could not parse a domain from the URL")
    if domain in {normalize_domain(d) for d in SITE_CONFIGS}:
        raise HTTPException(400, f"'{domain}' is a built-in site — it's already available")
    from src.site_configs import USER_SITES
    if domain not in USER_SITES and len(USER_SITES) >= MAX_USER_SITES:
        raise HTTPException(400, f"Site limit reached ({MAX_USER_SITES}). Remove a site first.")
    cfg = SiteFormConfig(
        domain=domain, url_template=url,
        name_field=req.name_field.strip(), email_field=req.email_field.strip(),
        url_field=req.url_field.strip(), city_field=req.city_field.strip(),
        message_field=req.message_field.strip(), captcha_field=req.captcha_field.strip(),
        submit_selector=req.submit_selector.strip(),
        success_keywords=[k for k in (req.success_keywords or []) if k.strip()],
        failure_keywords=[k for k in (req.failure_keywords or []) if k.strip()],
        auto_detect=True,
    )
    saved = add_user_site(cfg)
    logger.info(f"User added site: {saved.domain} ({url})")
    return {"status": "ok", "domain": saved.domain, "url": saved.url_template}


@app.delete("/api/sites/{domain}")
async def delete_site(domain: str):
    """Remove a user-added site. Built-in sites are immutable."""
    from src.site_configs import remove_user_site, SITE_CONFIGS, normalize_domain
    if normalize_domain(domain) in {normalize_domain(d) for d in SITE_CONFIGS}:
        raise HTTPException(400, "Built-in sites can't be removed")
    if remove_user_site(domain):
        return {"status": "ok"}
    raise HTTPException(404, "Site not found")


@app.post("/api/sites/detect")
async def detect_site(req: DetectRequest):
    """Load a URL, auto-detect its form fields, and return a preview + screenshot."""
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(400, "URL is required")
    if "://" not in url:
        url = "https://" + url
    ok, msg = await asyncio.to_thread(_validate_public_url, url)
    if not ok:
        raise HTTPException(400, msg)
    if DETECT_SEMAPHORE.locked():
        logger.info("Detect queue busy — waiting for a slot")
    async with DETECT_SEMAPHORE:
        try:
            return await _detect_site_fields(url)
        except Exception as e:
            logger.error(f"Detect failed for {url}: {e}")
            raise HTTPException(502, f"Could not load or analyze the page: {str(e)[:120]}")


@app.post("/api/submit")
async def submit_backlinks(req: BacklinkRequest):
    global BACKLINK_QUEUE, RESULTS, PROCESSING, SCREENSHOTS_ENABLED, CURRENT_RUN_ID

    if not req.urls:
        raise HTTPException(400, "No URLs provided")

    # SSRF guard: never let the headless browser be pointed at internal hosts.
    for target in req.urls:
        ok, msg = await asyncio.to_thread(_validate_public_url, target)
        if not ok:
            raise HTTPException(400, f"{target}: {msg}")

    # Support multiple client backlink URLs (one per line, or comma-separated).
    backlinks = [b.strip() for b in re.split(r"[\n,]+", req.backlink or "") if b.strip()]
    # Normalize to scheme-qualified URLs. Sites that validate the homepage/website
    # field as a URL (Ilch CMS — geini.de / ersterzug-hq.com) REJECT a bare
    # "domain.com" with "Webseite muss eine gültige URL sein". Prepend https:// when
    # the user omits the scheme so the URL passes validation. Idempotent.
    backlinks = [
        b if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", b) else "https://" + b
        for b in backlinks
    ]
    if not backlinks:
        raise HTTPException(400, "No backlink URL provided")

    async with LOCK:
        if PROCESSING:
            raise HTTPException(409, "Already processing a batch — wait for it to finish")

        SCREENSHOTS_ENABLED = bool(req.screenshots)
        RESULTS = []
        BACKLINK_QUEUE = []
        # One submission per target site. ALL client backlink URLs are posted
        # together in that single submission (listed one per line in the comment
        # body) — NOT one submission per URL. So N URLs x M sites => M submissions.
        backlink_display = ", ".join(backlinks)
        for url in req.urls:
            RESULTS.append({
                "url": url,
                "backlink": backlink_display,
                "status": "queued",
                "message": "Waiting...",
                "captcha_token": "",
                "screenshot": "",
                "screenshot_before": "",
                "elapsed_ms": 0,
            })
            BACKLINK_QUEUE.append({"url": url, "backlinks": backlinks})
        PROCESSING = True
        # Open a history record for this run (persisted incrementally as it runs).
        CURRENT_RUN_ID = f"{int(time.time() * 1000)}"
        _upsert_current_run()

    asyncio.create_task(_process_queue())
    return {"status": "ok", "count": len(BACKLINK_QUEUE)}


async def broadcast_state():
    """Push current state to all connected WebSocket clients."""
    data = {
        "type": "state",
        "results": RESULTS,
        "progress": {
            "done": len([r for r in RESULTS if r["status"] != "processing"]),
            "total": max(len(RESULTS), 1),
        },
        "processing": PROCESSING,
    }
    disconnected = []
    for ws in list(WS_CLIENTS):
        try:
            await ws.send_json(data)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in WS_CLIENTS:
            WS_CLIENTS.remove(ws)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    WS_CLIENTS.append(ws)
    logger.info(f"WebSocket client connected (total: {len(WS_CLIENTS)})")
    try:
        # Send initial state immediately
        await broadcast_state()
        # Keep connection alive, listening for pings
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=30)
                if msg == "ping":
                    await ws.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                # Send keepalive
                await ws.send_json({"type": "keepalive"})
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        if ws in WS_CLIENTS:
            WS_CLIENTS.remove(ws)
        logger.info(f"WebSocket client disconnected (total: {len(WS_CLIENTS)})")


async def _process_queue():
    global BACKLINK_QUEUE, RESULTS, PROCESSING

    from playwright.async_api import async_playwright
    from src.models import CaptchaType, CaptchaChallenge
    from src.solvers.ocr import OCRSolver
    from src.utils.image import decode_image
    from src.utils.model_manager import ModelManager
    from src.site_configs import get_config_or_generic, get_random_profile

    pw = None
    browser = None
    model_manager = None
    try:
        # Startup is INSIDE the try so a launch/model failure still hits the
        # finally that clears PROCESSING — otherwise the server would be stuck
        # returning 409 "already processing" forever until a manual restart.
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        model_manager = ModelManager(gpu=True)

        # Bound disk usage before a new batch adds more images.
        _prune_dir(Path("screenshots"), keep=300)
        _prune_dir(Path("screenshots/captcha_debug"), keep=300)

        for idx, item in enumerate(BACKLINK_QUEUE):
            url = item["url"]
            backlinks = item["backlinks"]
            domain = url.split("/")[2].replace("www.", "")

            # Mark as processing
            RESULTS[idx]["status"] = "processing"
            RESULTS[idx]["message"] = "Starting..."
            await broadcast_state()

            # Never None: unknown sites get a generic auto-detect config so the
            # pipeline always runs. The built-in 5 keep their hand-tuned mappings.
            config = get_config_or_generic(url)
            profile = get_random_profile()

            # Context creation wrapped so a browser-level failure marks THIS item
            # failed and continues to the next — it must not abandon the batch.
            try:
                ctx = await browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="America/New_York",
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                    ignore_https_errors=True,
                )
                await _attach_ssrf_guard(ctx)
                page = await ctx.new_page()
            except Exception as ctx_err:
                logger.error(f"Failed to open browser context for {domain}: {ctx_err}")
                RESULTS[idx]["status"] = "failed"
                RESULTS[idx]["message"] = "Browser error — skipped"
                await broadcast_state()
                continue

            try:
                start = time.time()

                # Navigate with retry for transient network errors
                RESULTS[idx]["message"] = "Loading page..."
                for _nav_try in range(3):
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        break
                    except Exception as nav_err:
                        err_str = str(nav_err)
                        if _nav_try < 2 and ("ERR_NAME_NOT_RESOLVED" in err_str or "ERR_CONNECTION" in err_str or "ERR_TIMED_OUT" in err_str):
                            logger.warning(f"Navigation retry {_nav_try+1}/3 for {domain}: {err_str[:80]}")
                            await asyncio.sleep(3)
                            continue
                        raise
                await _human_delay(1500, 2500)

                # Natural scrolling
                await _human_scroll(page)
                await _human_delay(500, 1200)

                # Baseline for definitive success detection: how many times does the
                # client's backlink already appear on this page (from prior entries)?
                # Captured BEFORE filling the form so the form's own copy of the URL
                # does not inflate the baseline. A NEW occurrence after submit means
                # our entry was actually posted (works even when the site shows no
                # success banner). Sites requiring moderation won't render it, so this
                # is a positive-only signal — its absence never implies failure.
                backlink_frag = ""
                if backlinks:
                    backlink_frag = re.sub(r"^https?://(www\.)?", "", backlinks[0].strip().rstrip("/")).lower()
                backlink_count_before = 0
                if backlink_frag:
                    backlink_count_before = await _count_backlink_occurrences(page, backlink_frag)

                # Baseline any error/notice element ALREADY visible before we submit
                # (e.g. a persistent cookie/GDPR banner using .alert-danger/[role=alert]).
                # Only a NEW error after submit counts as a rejection — otherwise a
                # permanent banner would fail every post on a user-added site.
                visible_error_before = ""
                if getattr(config, "auto_detect", False):
                    visible_error_before = await _detect_visible_error(page)

                # For unknown/user-added sites, resolve empty field names from the
                # live DOM so the generic pipeline knows which inputs to fill. The
                # built-in 5 (auto_detect=False) are returned unchanged.
                config = await _augment_config_from_dom(page, config)

                # Fill non-captcha form fields FIRST, then solve captcha right
                # before filling it — minimizes delay between solve and submit
                # (some sites expire captchas quickly or refresh on interaction).
                img_selectors = [
                    "img[src*='captcha']", "img[class*='captcha']", "img[id*='captcha']",
                    "img[src*='Captcha']", "img[src*='kod']", "img[src*='tmp/']",
                    "img[alt*='captcha']", "img[alt*='Captcha']",
                    "img[src*='securimage']", "img[src*='verify']",
                    ".captcha img",
                    "img[src*='index.php'][src*='captcha']",
                ]
                captcha_token = ""

                if config:
                    RESULTS[idx]["message"] = "Filling form..."
                    await _fill_form_humanized(page, config, profile, "", backlinks, skip_captcha=True)

                # NOW extract and solve captcha (after other fields are filled)
                captcha_b64 = None
                for sel in img_selectors:
                    try:
                        img_el = page.locator(sel).first
                        if await img_el.count() > 0:
                            canvas_b64 = await page.evaluate("""(selector) => {
                                const img = document.querySelector(selector);
                                if (!img || !img.complete || !img.naturalWidth) return null;
                                const canvas = document.createElement('canvas');
                                canvas.width = img.naturalWidth;
                                canvas.height = img.naturalHeight;
                                const ctx = canvas.getContext('2d');
                                ctx.drawImage(img, 0, 0);
                                try { return canvas.toDataURL('image/png').split(',')[1]; }
                                catch(e) { return null; }
                            }""", sel)
                            if canvas_b64:
                                captcha_b64 = canvas_b64
                                break
                            captcha_screenshot = await img_el.screenshot(type="png")
                            captcha_b64 = base64.b64encode(captcha_screenshot).decode()
                            break
                    except Exception:
                        continue

                if not captcha_b64:
                    try:
                        captcha_b64 = await page.evaluate("""() => {
                            const inp = document.querySelector("input[name*='captcha']");
                            if (!inp) return null;
                            let container = inp.parentElement;
                            for (let i = 0; i < 3 && container; i++) {
                                const img = container.querySelector('img');
                                if (img && img.src && !img.src.includes('data:image/gif')) {
                                    const canvas = document.createElement('canvas');
                                    canvas.width = img.naturalWidth || img.width;
                                    canvas.height = img.naturalHeight || img.height;
                                    const ctx = canvas.getContext('2d');
                                    ctx.drawImage(img, 0, 0);
                                    try { return canvas.toDataURL('image/png').split(',')[1]; }
                                    catch(e) { return null; }
                                }
                                container = container.parentElement;
                            }
                            return null;
                        }""")
                    except Exception:
                        pass

                if captcha_b64:
                    RESULTS[idx]["message"] = "Solving captcha..."
                    solver = OCRSolver(model_manager=model_manager)

                    # Extract Yii2 captcha hash for hash-validated OCR
                    captcha_hash = await _extract_yii2_hash(page)

                    challenge = CaptchaChallenge(
                        type=CaptchaType.IMAGE_CAPTCHA,
                        page_url=url,
                        extra={"image_data": captcha_b64, "captcha_hash": captcha_hash},
                    )
                    solution = await solver.solve(challenge)
                    if solution.success:
                        captcha_token = solution.token
                        RESULTS[idx]["captcha_token"] = captcha_token

                        # Preventive refresh loop: if OCR is uncertain and site is Yii2,
                        # keep refreshing captcha (up to 3 times) until we get a high-confidence match
                        if solution.confidence < 0.85 and captcha_hash is not None:
                            max_refreshes = 3
                            for refresh_i in range(max_refreshes):
                                logger.info(f"Low OCR confidence ({solution.confidence:.2f}) — preventive captcha refresh {refresh_i+1}/{max_refreshes} for {domain}")
                                try:
                                    cimg_el = page.locator('#comment-form-new-verifycode-image').first
                                    if await cimg_el.count() == 0:
                                        break
                                    await cimg_el.click()
                                    await _human_delay(1500, 2500)
                                    await page.wait_for_function("""() => {
                                        const img = document.getElementById('comment-form-new-verifycode-image');
                                        return img && img.complete && img.naturalWidth > 0;
                                    }""", timeout=5000)
                                    await _human_delay(500, 800)
                                    fresh_b64 = await page.evaluate("""() => {
                                        const img = document.getElementById('comment-form-new-verifycode-image');
                                        if (!img || !img.complete || !img.naturalWidth) return null;
                                        const c = document.createElement('canvas');
                                        c.width = img.naturalWidth; c.height = img.naturalHeight;
                                        c.getContext('2d').drawImage(img, 0, 0);
                                        try { return c.toDataURL('image/png').split(',')[1]; }
                                        catch(e) { return null; }
                                    }""")
                                    if not fresh_b64:
                                        break
                                    fresh_hash = await _extract_yii2_hash(page)
                                    fresh_sol = await solver.solve(CaptchaChallenge(
                                        type=CaptchaType.IMAGE_CAPTCHA, page_url=url,
                                        extra={"image_data": fresh_b64, "captcha_hash": fresh_hash},
                                    ))
                                    if fresh_sol.success:
                                        old_token = captcha_token
                                        captcha_token = fresh_sol.token
                                        solution = fresh_sol
                                        RESULTS[idx]["captcha_token"] = captcha_token
                                        logger.info(f"Preventive refresh {refresh_i+1}: '{old_token}' -> '{captcha_token}' (conf {solution.confidence:.2f})")
                                        if fresh_sol.confidence >= 0.85:
                                            break
                                except Exception as e:
                                    logger.warning(f"Preventive refresh {refresh_i+1} failed: {e}")
                                    break
                    else:
                        RESULTS[idx]["status"] = "failed"
                        RESULTS[idx]["message"] = f"Captcha failed: {solution.error}"
                        await ctx.close()
                        continue
                else:
                    has_recaptcha = await page.evaluate("typeof grecaptcha !== 'undefined'")
                    if not has_recaptcha:
                        captcha_token = "no_captcha"

                # Fill the captcha field now (right before submit)
                if config and captcha_token:
                    await _fill_captcha_only(page, config, captcha_token)
                    await _human_delay(500, 1500)

                    # Take BEFORE screenshot to verify form fill (if enabled)
                    if SCREENSHOTS_ENABLED:
                        try:
                            Path("screenshots").mkdir(exist_ok=True)
                            before_path = f"screenshots/before_{domain}_{int(time.time())}.png"
                            await page.screenshot(path=before_path, full_page=True)
                            RESULTS[idx]["screenshot_before"] = f"/screenshots/{Path(before_path).name}"
                        except Exception:
                            pass

                    # Capture dialog messages and comment count before submit
                    dialog_messages = []
                    def _on_dialog(dialog):
                        dialog_messages.append(dialog.message)
                        asyncio.ensure_future(dialog.accept())
                    page.on("dialog", _on_dialog)

                    comment_count_before = 0
                    try:
                        comment_count_before = await page.evaluate("""() => {
                            const comments = document.querySelectorAll('.comment, .visitor-comment, [class*="comment"], tr[class*="row"], .guestbook-entry');
                            return comments.length;
                        }""")
                    except Exception:
                        pass

                    # Submit
                    RESULTS[idx]["message"] = "Submitting..."
                    submit_ok = await _click_submit_humanized(page, config)
                    if not submit_ok:
                        RESULTS[idx]["status"] = "failed"
                        RESULTS[idx]["message"] = "Submit button not found or click failed"
                        page.remove_listener("dialog", _on_dialog)
                        await ctx.close()
                        continue

                    # Wait for response with multiple checks
                    RESULTS[idx]["message"] = "Waiting for confirmation..."
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass
                    await _human_delay(1500, 3000)

                    # Check for dialog messages (JS alerts with error info)
                    if dialog_messages:
                        logger.info(f"Dialog messages for {domain}: {dialog_messages}")

                    # Scroll to load lazy content
                    try:
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await _human_delay(500, 1000)
                    except Exception:
                        await _human_delay(1000, 1500)

                    page.remove_listener("dialog", _on_dialog)

                    # Detect result from full page text
                    try:
                        body = await page.locator("body").inner_text()
                    except Exception:
                        body = ""
                    body_lower = body.lower()

                    # Check dialog messages for failure/success indicators
                    for msg in dialog_messages:
                        msg_lower = msg.lower()
                        for kw in config.failure_keywords:
                            if kw.lower() in msg_lower:
                                body_lower = body_lower + " " + msg_lower
                                break

                    # 1) Look for failure evidence FIRST — it overrides success.
                    fail_hit = ""
                    for kw in config.failure_keywords:
                        if kw.lower() in body_lower:
                            fail_hit = kw
                            break
                    if not fail_hit:
                        for kw in GENERIC_ERROR_PHRASES:
                            if kw in body_lower:
                                fail_hit = kw
                                break
                    # Generic sites carry no failure-keyword list, so also look for a
                    # visible validation-error element (language-independent). Scoped
                    # to auto-detect sites so the tuned 5 are unaffected.
                    if not fail_hit and getattr(config, "auto_detect", False):
                        _err = await _detect_visible_error(page)
                        if _err and _err != visible_error_before:
                            fail_hit = _err
                            logger.info(f"NEW visible error element for {domain}: {_err}")

                    # STRONGEST signal: a NEW entry containing the client's backlink
                    # appeared (count increased vs the pre-fill baseline, form values
                    # excluded). Definitive — a real posted entry, so it OVERRIDES an
                    # error banner (some sites show both a post and a stray notice).
                    backlink_increased = False
                    if backlink_frag:
                        try:
                            _after = await _count_backlink_occurrences(page, backlink_frag)
                            if _after > backlink_count_before:
                                backlink_increased = True
                                logger.info(f"Backlink now appears {_after}x (was {backlink_count_before}) — confirmed post for {domain}")
                        except Exception:
                            pass

                    # 2) Only consider success signals when there is NO error banner,
                    #    unless the backlink itself confirms the post.
                    success = False
                    if backlink_increased:
                        success = True
                        fail_hit = ""  # a real posted entry trumps any error text
                    if not fail_hit and not success:
                        for kw in config.success_keywords:
                            if kw.lower() in body_lower:
                                success = True
                                logger.info(f"Success keyword found: '{kw}' for {domain}")
                                break

                        # If page URL changed (redirect), treat as likely success
                        if not success and not body_lower:
                            try:
                                current_url = page.url
                                if current_url != url:
                                    success = True
                                    logger.info(f"Page redirected to {current_url} — treating as success for {domain}")
                            except Exception:
                                pass
                        # NOTE: the old "profile name appears in body => success" heuristic
                        # was removed — the 30-name pool collides with names already in the
                        # guestbook history, causing cross-run false positives. The backlink
                        # count-increase above is the reliable run-specific confirmation.

                        # Yii2 form (Kazan): if captcha field exists but has NO error
                        # class AND no visible error-summary, the form was accepted
                        if not success:
                            try:
                                yii2_status = await page.evaluate("""() => {
                                    const captchaField = document.querySelector('.field-comment-form-new-verifycode');
                                    if (!captchaField) return 'no_element';
                                    if (captchaField.classList.contains('has-error')) return 'has_error';
                                    const summary = document.querySelector('.error-summary');
                                    if (summary && summary.style.display !== 'none' && summary.offsetParent !== null) return 'has_error';
                                    return 'clean';
                                }""")
                                if yii2_status == 'clean':
                                    success = True
                                    logger.info(f"Yii2 form submitted with no errors — success for {domain}")
                            except Exception:
                                pass

                    # Yii2 error override: a visible error-summary trumps the weaker
                    # keyword/Yii2-clean success signals — but NOT a confirmed backlink
                    # post (that is a real entry, stronger than any error banner).
                    if success and not backlink_increased:
                        try:
                            yii2_override = await page.evaluate("""() => {
                                const cf = document.querySelector('.field-comment-form-new-verifycode');
                                if (cf && cf.classList.contains('has-error')) return 'has_error';
                                const s = document.querySelector('.error-summary');
                                if (s && s.style.display !== 'none' && s.offsetParent !== null) return 'has_error';
                                return 'ok';
                            }""")
                            if yii2_override == 'has_error':
                                success = False
                                fail_hit = fail_hit or "form error-summary"
                                logger.info(f"Yii2 error override: error visible despite success match — {domain}")
                        except Exception:
                            pass

                    if success:
                        RESULTS[idx]["status"] = "success"
                        RESULTS[idx]["message"] = f"Posted! ({config.domain})"
                    else:
                        if fail_hit:
                            RESULTS[idx]["status"] = "failed"
                            RESULTS[idx]["message"] = f"Rejected by site"
                            logger.info(f"Failure detected ('{fail_hit}') for {domain}")
                        else:
                            # Check if comment count increased (works for sites with no success message)
                            comment_added = False
                            if comment_count_before > 0:
                                try:
                                    comment_count_after = await page.evaluate("""() => {
                                        const comments = document.querySelectorAll('.comment, .visitor-comment, [class*="comment"], tr[class*="row"], .guestbook-entry');
                                        return comments.length;
                                    }""")
                                    if comment_count_after > comment_count_before:
                                        comment_added = True
                                        logger.info(f"Comment count increased {comment_count_before} -> {comment_count_after} for {domain}")
                                except Exception:
                                    pass
                            if comment_added:
                                RESULTS[idx]["status"] = "success"
                                RESULTS[idx]["message"] = f"Posted! ({config.domain})"
                            else:
                                RESULTS[idx]["status"] = "unknown"
                                RESULTS[idx]["message"] = f"Submitted but couldn't verify result"
                                logger.info(f"No success/failure keywords matched for {domain}")

                    # Take full page screenshot for verification (if enabled)
                    if SCREENSHOTS_ENABLED:
                        try:
                            screenshot_path = f"screenshots/backlink_{domain}_{int(time.time())}.png"
                            Path("screenshots").mkdir(exist_ok=True)
                            await page.screenshot(path=screenshot_path, full_page=True)
                            RESULTS[idx]["screenshot"] = f"/screenshots/{Path(screenshot_path).name}"
                        except Exception:
                            pass

                    # Retry: if captcha was likely wrong, try again with fresh captcha.
                    # starwars math captchas are heavily strike-through-obscured and
                    # only verifiable post-submit, so give them more re-roll attempts —
                    # each retry fetches a fresh, possibly-readable captcha.
                    if RESULTS[idx]["status"] in ("failed", "unknown") and RESULTS[idx].get("captcha_token"):
                        if "starwars" in domain or "freakz" in domain:
                            max_retries = 7          # 8 total (noisy math captcha)
                        elif "ersterzug" in domain or "geini" in domain:
                            max_retries = 9          # 10 total (hard Ilch distorted text)
                        else:
                            max_retries = 3          # 4 total
                        total_attempts = max_retries + 1
                        for _retry in range(max_retries):
                            logger.info(f"Retrying {domain} with new captcha (attempt {_retry + 2}/{total_attempts})")
                            RESULTS[idx]["message"] = f"Retrying captcha (attempt {_retry + 2}/{total_attempts})..."
                            RESULTS[idx]["status"] = "processing"
                            await broadcast_state()

                            # Yii2 fast retry: refresh captcha in-place, no page reload
                            is_yii2 = "kazan" in domain or "top100lingua" in domain
                            if is_yii2:
                                try:
                                    captcha_img_el = page.locator('#comment-form-new-verifycode-image').first
                                    if await captcha_img_el.count() > 0:
                                        await captcha_img_el.click()
                                        await _human_delay(1500, 2500)
                                        await page.wait_for_function("""() => {
                                            const img = document.getElementById('comment-form-new-verifycode-image');
                                            return img && img.complete && img.naturalWidth > 0;
                                        }""", timeout=5000)
                                        await _human_delay(500, 1000)
                                        yii2_b64 = await page.evaluate("""() => {
                                            const img = document.getElementById('comment-form-new-verifycode-image');
                                            if (!img || !img.complete || !img.naturalWidth) return null;
                                            const c = document.createElement('canvas');
                                            c.width = img.naturalWidth; c.height = img.naturalHeight;
                                            c.getContext('2d').drawImage(img, 0, 0);
                                            try { return c.toDataURL('image/png').split(',')[1]; }
                                            catch(e) { return null; }
                                        }""")
                                        if yii2_b64:
                                            yii2_hash = await _extract_yii2_hash(page)
                                            sol = await solver.solve(CaptchaChallenge(
                                                type=CaptchaType.IMAGE_CAPTCHA, page_url=url,
                                                extra={"image_data": yii2_b64, "captcha_hash": yii2_hash},
                                            ))
                                            for _rr in range(2):
                                                if not sol.success or sol.confidence >= 0.85:
                                                    break
                                                logger.info(f"Fast retry low conf ({sol.confidence:.2f}) — extra refresh for {domain}")
                                                try:
                                                    await captcha_img_el.click()
                                                    await _human_delay(1200, 2000)
                                                    await page.wait_for_function("""() => {
                                                        const img = document.getElementById('comment-form-new-verifycode-image');
                                                        return img && img.complete && img.naturalWidth > 0;
                                                    }""", timeout=5000)
                                                    await _human_delay(400, 700)
                                                    rr_b64 = await page.evaluate("""() => {
                                                        const img = document.getElementById('comment-form-new-verifycode-image');
                                                        if (!img || !img.complete || !img.naturalWidth) return null;
                                                        const c = document.createElement('canvas');
                                                        c.width = img.naturalWidth; c.height = img.naturalHeight;
                                                        c.getContext('2d').drawImage(img, 0, 0);
                                                        try { return c.toDataURL('image/png').split(',')[1]; }
                                                        catch(e) { return null; }
                                                    }""")
                                                    if rr_b64:
                                                        rr_hash = await _extract_yii2_hash(page)
                                                        sol = await solver.solve(CaptchaChallenge(
                                                            type=CaptchaType.IMAGE_CAPTCHA, page_url=url,
                                                            extra={"image_data": rr_b64, "captcha_hash": rr_hash},
                                                        ))
                                                except Exception:
                                                    break
                                            if sol.success:
                                                captcha_token = sol.token
                                                RESULTS[idx]["captcha_token"] = captcha_token
                                                await page.fill('input[name="comment-form-new[verifyCode]"]', sol.token)
                                                await _human_delay(300, 800)
                                                await page.locator('button.comment-submit, button[type=submit]').first.click()
                                                try:
                                                    await page.wait_for_load_state("networkidle", timeout=15000)
                                                except Exception:
                                                    pass
                                                await _human_delay(2000, 4000)
                                                try:
                                                    body = await page.locator("body").inner_text()
                                                except Exception:
                                                    body = ""
                                                body_lower = body.lower()
                                                yii2_clean = False
                                                try:
                                                    yii2_s = await page.evaluate("""() => {
                                                        const cf = document.querySelector('.field-comment-form-new-verifycode');
                                                        if (!cf) return 'no_element';
                                                        if (cf.classList.contains('has-error')) return 'has_error';
                                                        const s = document.querySelector('.error-summary');
                                                        if (s && s.style.display !== 'none' && s.offsetParent !== null) return 'has_error';
                                                        return 'clean';
                                                    }""")
                                                    yii2_clean = yii2_s == 'clean'
                                                except Exception:
                                                    pass
                                                fail_kw = next((kw for kw in config.failure_keywords if kw.lower() in body_lower), None)
                                                if yii2_clean and not fail_kw:
                                                    RESULTS[idx]["status"] = "success"
                                                    RESULTS[idx]["message"] = f"Posted! ({config.domain})"
                                                    logger.info(f"Yii2 fast retry success for {domain}")
                                                elif fail_kw:
                                                    RESULTS[idx]["status"] = "failed"
                                                    RESULTS[idx]["message"] = "Rejected by site"
                                                    logger.info(f"Yii2 fast retry failed: '{fail_kw}' for {domain}")
                                                else:
                                                    RESULTS[idx]["status"] = "unknown"
                                                    RESULTS[idx]["message"] = "Submitted but couldn't verify"
                                                if SCREENSHOTS_ENABLED:
                                                    try:
                                                        sp = f"screenshots/backlink_{domain}_{int(time.time())}.png"
                                                        await page.screenshot(path=sp, full_page=True)
                                                        RESULTS[idx]["screenshot"] = f"/screenshots/{Path(sp).name}"
                                                    except Exception:
                                                        pass
                                                if RESULTS[idx]["status"] == "success":
                                                    break
                                                continue
                                except Exception as e:
                                    logger.warning(f"Yii2 fast retry failed: {e}")

                            # For session-based captcha sites, create a fresh browser context
                            # so we get a genuinely new captcha image
                            needs_fresh_ctx = "klubabstynenta" in domain or "piekary" in domain
                            retry_page = page
                            retry_ctx = None
                            if needs_fresh_ctx:
                                retry_ctx = await browser.new_context(
                                    viewport={"width": 1920, "height": 1080},
                                    locale="en-US",
                                    timezone_id="America/New_York",
                                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                                    ignore_https_errors=True,
                                )
                                retry_page = await retry_ctx.new_page()

                            try:
                                await retry_page.goto(url, wait_until="domcontentloaded", timeout=30000)
                                await _human_delay(2000, 4000)

                                new_captcha_b64 = None
                                for sel in img_selectors:
                                    try:
                                        cimg = retry_page.locator(sel).first
                                        if await cimg.count() > 0:
                                            canvas_b64 = await retry_page.evaluate("""(selector) => {
                                                const img = document.querySelector(selector);
                                                if (!img || !img.complete || !img.naturalWidth) return null;
                                                const canvas = document.createElement('canvas');
                                                canvas.width = img.naturalWidth;
                                                canvas.height = img.naturalHeight;
                                                const ctx = canvas.getContext('2d');
                                                ctx.drawImage(img, 0, 0);
                                                try { return canvas.toDataURL('image/png').split(',')[1]; }
                                                catch(e) { return null; }
                                            }""", sel)
                                            if canvas_b64:
                                                new_captcha_b64 = canvas_b64
                                                break
                                            new_captcha_b64 = base64.b64encode(
                                                await cimg.screenshot(type="png")
                                            ).decode()
                                            break
                                    except Exception:
                                        continue
                                if not new_captcha_b64:
                                    try:
                                        new_captcha_b64 = await retry_page.evaluate("""() => {
                                            const inp = document.querySelector("input[name*='captcha']");
                                            if (!inp) return null;
                                            let container = inp.parentElement;
                                            for (let i = 0; i < 3 && container; i++) {
                                                const img = container.querySelector('img');
                                                if (img && img.src && !img.src.includes('data:image/gif')) {
                                                    const canvas = document.createElement('canvas');
                                                    canvas.width = img.naturalWidth || img.width;
                                                    canvas.height = img.naturalHeight || img.height;
                                                    const c2d = canvas.getContext('2d');
                                                    c2d.drawImage(img, 0, 0);
                                                    try { return canvas.toDataURL('image/png').split(',')[1]; }
                                                    catch(e) { return null; }
                                                }
                                                container = container.parentElement;
                                            }
                                            return null;
                                        }""")
                                    except Exception:
                                        pass
                                if not new_captcha_b64:
                                    if retry_ctx:
                                        await retry_ctx.close()
                                    break

                                retry_hash = await _extract_yii2_hash(retry_page)
                                solution = await solver.solve(CaptchaChallenge(
                                    type=CaptchaType.IMAGE_CAPTCHA,
                                    page_url=url,
                                    extra={"image_data": new_captcha_b64, "captcha_hash": retry_hash},
                                ))
                                if solution.success and solution.confidence < 0.85 and retry_hash is not None:
                                    rr_el = retry_page.locator('#comment-form-new-verifycode-image').first
                                    for _rr2 in range(2):
                                        if solution.confidence >= 0.85:
                                            break
                                        try:
                                            if await rr_el.count() == 0:
                                                break
                                            logger.info(f"Retry low conf ({solution.confidence:.2f}) — extra refresh for {domain}")
                                            await rr_el.click()
                                            await _human_delay(1200, 2000)
                                            await retry_page.wait_for_function("""() => {
                                                const img = document.getElementById('comment-form-new-verifycode-image');
                                                return img && img.complete && img.naturalWidth > 0;
                                            }""", timeout=5000)
                                            await _human_delay(400, 700)
                                            rr_b64 = await retry_page.evaluate("""() => {
                                                const img = document.getElementById('comment-form-new-verifycode-image');
                                                if (!img || !img.complete || !img.naturalWidth) return null;
                                                const c = document.createElement('canvas');
                                                c.width = img.naturalWidth; c.height = img.naturalHeight;
                                                c.getContext('2d').drawImage(img, 0, 0);
                                                try { return c.toDataURL('image/png').split(',')[1]; }
                                                catch(e) { return null; }
                                            }""")
                                            if not rr_b64:
                                                break
                                            rr_hash = await _extract_yii2_hash(retry_page)
                                            solution = await solver.solve(CaptchaChallenge(
                                                type=CaptchaType.IMAGE_CAPTCHA, page_url=url,
                                                extra={"image_data": rr_b64, "captcha_hash": rr_hash},
                                            ))
                                            new_captcha_b64 = rr_b64
                                        except Exception:
                                            break
                                if not solution.success:
                                    if retry_ctx:
                                        await retry_ctx.close()
                                    continue
                                captcha_token = solution.token
                                RESULTS[idx]["captcha_token"] = captcha_token

                                new_profile = get_random_profile()
                                await _fill_form_humanized(retry_page, config, new_profile, "", backlinks, skip_captcha=True)

                                # Re-extract captcha in case form fill triggered a refresh
                                fresh_b64 = None
                                for sel in img_selectors:
                                    try:
                                        cimg2 = retry_page.locator(sel).first
                                        if await cimg2.count() > 0:
                                            fresh_b64 = await retry_page.evaluate("""(selector) => {
                                                const img = document.querySelector(selector);
                                                if (!img || !img.complete || !img.naturalWidth) return null;
                                                const canvas = document.createElement('canvas');
                                                canvas.width = img.naturalWidth;
                                                canvas.height = img.naturalHeight;
                                                const ctx = canvas.getContext('2d');
                                                ctx.drawImage(img, 0, 0);
                                                try { return canvas.toDataURL('image/png').split(',')[1]; }
                                                catch(e) { return null; }
                                            }""", sel)
                                            if fresh_b64:
                                                break
                                    except Exception:
                                        continue
                                if fresh_b64 and fresh_b64 != new_captcha_b64:
                                    logger.info("Captcha image changed after form fill — re-solving")
                                    fresh_hash = await _extract_yii2_hash(retry_page)
                                    solution = await solver.solve(CaptchaChallenge(
                                        type=CaptchaType.IMAGE_CAPTCHA,
                                        page_url=url,
                                        extra={"image_data": fresh_b64, "captcha_hash": fresh_hash},
                                    ))
                                    if solution.success:
                                        captcha_token = solution.token
                                        RESULTS[idx]["captcha_token"] = captcha_token

                                await _fill_captcha_only(retry_page, config, captcha_token)
                                await _human_delay(500, 1500)

                                if SCREENSHOTS_ENABLED:
                                  try:
                                    bp = f"screenshots/before_{domain}_{int(time.time())}.png"
                                    await retry_page.screenshot(path=bp, full_page=True)
                                    RESULTS[idx]["screenshot_before"] = f"/screenshots/{Path(bp).name}"
                                  except Exception:
                                    pass

                                RESULTS[idx]["message"] = "Submitting..."
                                submit_ok = await _click_submit_humanized(retry_page, config)
                                if not submit_ok:
                                    RESULTS[idx]["status"] = "failed"
                                    RESULTS[idx]["message"] = "Submit failed on retry"
                                    if retry_ctx:
                                        await retry_ctx.close()
                                    break

                                try:
                                    await retry_page.wait_for_load_state("networkidle", timeout=15000)
                                except Exception:
                                    pass
                                await _human_delay(3000, 6000)

                                try:
                                    body = await retry_page.locator("body").inner_text()
                                except Exception:
                                    body = ""
                                body_lower = body.lower()

                                success_kw = next((kw for kw in config.success_keywords if kw.lower() in body_lower), None)
                                fail_kw = next((kw for kw in config.failure_keywords if kw.lower() in body_lower), None)
                                if not fail_kw:
                                    fail_kw = next((kw for kw in GENERIC_ERROR_PHRASES if kw in body_lower), None)
                                if not fail_kw and getattr(config, "auto_detect", False):
                                    _err2 = await _detect_visible_error(retry_page)
                                    fail_kw = _err2 if (_err2 and _err2 != visible_error_before) else None
                                # Definitive: a new occurrence of the backlink URL vs the
                                # pre-fill baseline (form values excluded, so an error
                                # re-render echoing our input can't create a false post).
                                _after2 = await _count_backlink_occurrences(retry_page, backlink_frag) if backlink_frag else 0
                                backlink_increased = bool(backlink_frag) and _after2 > backlink_count_before

                                # Yii2: check if the captcha field exists with no error
                                yii2_no_error = False
                                yii2_s = ""
                                try:
                                    yii2_s = await retry_page.evaluate("""() => {
                                        const captchaField = document.querySelector('.field-comment-form-new-verifycode');
                                        if (!captchaField) return 'no_element';
                                        if (captchaField.classList.contains('has-error')) return 'has_error';
                                        const summary = document.querySelector('.error-summary');
                                        if (summary && summary.style.display !== 'none' && summary.offsetParent !== null) return 'has_error';
                                        return 'clean';
                                    }""")
                                    yii2_no_error = yii2_s == 'clean'
                                except Exception:
                                    pass

                                # A visible error (keyword, generic phrase, or Yii2
                                # error-summary) overrides any success/name match.
                                yii2_has_error = yii2_s == 'has_error'
                                if backlink_increased:
                                    # A real posted entry — strongest signal, wins over any error text.
                                    RESULTS[idx]["status"] = "success"
                                    RESULTS[idx]["message"] = f"Posted! ({config.domain})"
                                    logger.info(f"Retry success (backlink confirmed) for {domain}")
                                elif fail_kw or yii2_has_error:
                                    RESULTS[idx]["status"] = "failed"
                                    RESULTS[idx]["message"] = "Rejected by site"
                                    logger.info(f"Retry failure: '{fail_kw or 'form error-summary'}' for {domain}")
                                elif success_kw or yii2_no_error:
                                    RESULTS[idx]["status"] = "success"
                                    RESULTS[idx]["message"] = f"Posted! ({config.domain})"
                                    logger.info(f"Retry success: kw='{success_kw}' yii2_clean={yii2_no_error} for {domain}")
                                else:
                                    RESULTS[idx]["status"] = "unknown"
                                    RESULTS[idx]["message"] = "Submitted but couldn't verify result"
                                    logger.info(f"Retry: no keywords matched for {domain}")

                                if SCREENSHOTS_ENABLED:
                                  try:
                                    sp = f"screenshots/backlink_{domain}_{int(time.time())}.png"
                                    await retry_page.screenshot(path=sp, full_page=True)
                                    RESULTS[idx]["screenshot"] = f"/screenshots/{Path(sp).name}"
                                  except Exception:
                                    pass
                            finally:
                                if retry_ctx:
                                    await retry_ctx.close()

                            if RESULTS[idx]["status"] != "failed":
                                break
                else:
                    RESULTS[idx]["status"] = "failed"
                    RESULTS[idx]["message"] = f"No config for {domain}"

                elapsed = (time.time() - start) * 1000
                RESULTS[idx]["elapsed_ms"] = elapsed

            except Exception as e:
                RESULTS[idx]["status"] = "failed"
                RESULTS[idx]["message"] = str(e)[:100]
            finally:
                await ctx.close()

            await broadcast_state()
            # Persist this run's progress after every site — so even if the process
            # dies mid-batch (e.g. OOM), the History tab still shows what completed.
            _save_results()
            _upsert_current_run()

            # Brief delay between sites
            await _human_delay(1500, 3000)

    except Exception as batch_err:
        logger.error(f"Batch processing aborted: {batch_err}")
    finally:
        try:
            if model_manager is not None:
                model_manager.unload_all()
        except Exception:
            pass
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass
        try:
            if pw is not None:
                await pw.stop()
        except Exception:
            pass
        PROCESSING = False
        _save_results()
        _upsert_current_run(finished=True)
        await broadcast_state()


async def _extract_yii2_hash(page) -> int | None:
    """Extract captcha validation hash from Yii2 ActiveForm on the page."""
    try:
        result = await page.evaluate(r"""() => {
            if (typeof jQuery === 'undefined') return null;
            // Try all forms on the page
            const forms = document.querySelectorAll('form');
            for (const form of forms) {
                try {
                    const data = jQuery(form).data('yiiActiveForm');
                    if (!data || !data.attributes) continue;
                    for (const attr of data.attributes) {
                        if (!attr.validate || !attr.id) continue;
                        if (!attr.id.includes('captcha') && !attr.id.includes('verifycode')) continue;
                        // Extract hash from validate function source
                        const fnSrc = attr.validate.toString();
                        const hashMatch = fnSrc.match(/hash["']?\s*:\s*(-?\d+)/);
                        if (hashMatch) {
                            const optionsHash = parseInt(hashMatch[1]);
                            // Also check body data (set after captcha refresh)
                            const hashKeyMatch = fnSrc.match(/hashKey["']?\s*:\s*["']([^"']+)["']/);
                            if (hashKeyMatch) {
                                const bodyHash = jQuery('body').data(hashKeyMatch[1]);
                                if (bodyHash && Array.isArray(bodyHash)) {
                                    return bodyHash[1]; // case-insensitive hash
                                }
                            }
                            return optionsHash;
                        }
                    }
                } catch(e) {}
            }
            return null;
        }""")
        if result is not None:
            logger.info(f"Yii2 captcha hash extracted: {result}")
        return result
    except Exception as e:
        logger.debug(f"Yii2 hash extraction failed: {e}")
        return None


async def _fill_captcha_only(page, config, captcha_token):
    """Fill just the captcha field — called right before submit for freshness."""
    import random as _random
    if config.captcha_hash_field:
        form_scope = page.locator(
            f"form:has(input[name='{config.captcha_field}']), "
            f"form:has(input[name='{config.captcha_hash_field}'])"
        ).last
    else:
        form_scope = page.locator(
            f"form:has(input[name='{config.captcha_field}'])"
        ).last
    use_form = await form_scope.count() > 0

    def locate(selector):
        if use_form:
            return form_scope.locator(selector).first
        return page.locator(selector).first

    try:
        el = locate(f"input[name='{config.captcha_field}'], textarea[name='{config.captcha_field}']")
        if await el.count() > 0 and await el.is_visible():
            await el.click(delay=_random.randint(100, 300))
            await _human_delay(200, 400)
            await el.fill(captcha_token, timeout=5000)
            actual = await el.input_value()
            logger.info(f"CAPTCHA filled: '{actual}' (expected '{captcha_token}')")
            if actual != captcha_token:
                logger.error(f"CAPTCHA MISMATCH: expected '{captcha_token}' got '{actual}'")
                await el.fill(captcha_token, timeout=5000)
    except Exception as e:
        logger.error(f"Error filling captcha: {e}")


def _compose_backlink_content(comment, backlinks):
    """Build the comment body and the single homepage URL from the client URLs.

    ALL client URLs are listed one-per-line under the comment text; the first
    URL is returned separately for the site's single homepage/url input field.
    Accepts a list of URLs or a bare string (backwards-compat).

    Returns (message_text, primary_link, normalized_backlinks).
    """
    if isinstance(backlinks, str):
        backlinks = [backlinks] if backlinks.strip() else []
    backlinks = [b.strip() for b in (backlinks or []) if b and b.strip()]
    links_block = "\n".join(backlinks)                 # all URLs, one per line
    primary_link = backlinks[0] if backlinks else ""   # single homepage/url field
    message_text = f"{comment}\n\n{links_block}" if links_block else comment
    return message_text, primary_link, backlinks


async def _count_backlink_occurrences(page, frag: str) -> int:
    """Count how many times the backlink fragment appears in the page's rendered
    TEXT, EXCLUDING the values of form controls.

    This is the definitive success signal (a new count = a newly-posted entry).
    Excluding <textarea>/<input>/<select> matters because a validation error often
    re-renders the form with our own input preserved — the backlink we typed would
    otherwise be counted and mis-read as a fresh post (a false success on sites
    that have no failure-keyword list, i.e. every user-added site)."""
    if not frag:
        return 0
    try:
        return await page.evaluate(
            """(frag) => {
                const clone = document.body.cloneNode(true);
                // Strip form controls, WYSIWYG editor regions (contenteditable /
                // CKEditor / TinyMCE / Quill), and live-preview / alert / error
                // containers. Any of these can echo the URL we just typed; counting
                // that echo would fake a "posted" entry (false success), especially
                // on a REJECTED submit where the editor re-renders our input.
                clone.querySelectorAll(
                    'textarea, input, select, script, style, noscript, template, ' +
                    '[contenteditable], .ck-content, .cke_editable, .mce-content-body, .ql-editor, ' +
                    '.preview, .vorschau, [class*="preview"], .alert, [role=alert], .error, .errors'
                ).forEach(e => e.remove());
                const text = (clone.textContent || '').toLowerCase();
                if (!frag) return 0;
                let n = 0, i = 0;
                while ((i = text.indexOf(frag, i)) !== -1) { n++; i += frag.length; }
                return n;
            }""",
            frag,
        )
    except Exception:
        try:
            body = (await page.locator("body").inner_text()).lower()
            return body.count(frag)
        except Exception:
            return 0


async def _detect_visible_error(page) -> str:
    """Return a short label if the page shows a VISIBLE, non-empty validation/error
    element, else "". Language-independent — used only for auto-detected (user)
    sites, which have no per-site failure-keyword list. The built-in 5 keep their
    tuned keyword + Yii2 checks, so this can't regress them."""
    try:
        return await page.evaluate(
            """() => {
                const sels = [
                    '.error', '.errors', '.has-error', '.is-invalid', '.invalid-feedback',
                    '.alert-danger', '.alert-error', '.form-error', '.field-error',
                    '.error-summary', '.error-message', '.validation-summary-errors',
                    '.wpcf7-not-valid-tip', '[role=alert]',
                ];
                for (const sel of sels) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (el.offsetParent === null) continue;             // not rendered
                        const st = window.getComputedStyle(el);
                        if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') === 0) continue;
                        const txt = (el.innerText || el.textContent || '').trim();
                        if (txt.length >= 3) return sel + ': ' + txt.slice(0, 60);
                    }
                }
                return '';
            }"""
        )
    except Exception:
        return ""


# JS run in the page to infer which inputs are name/email/url/city/message/captcha
# on an unknown site. Scoped to the form that carries the captcha (or the last form
# with a textarea) so login/search boxes elsewhere on the page are ignored.
_FIELD_DETECT_JS = r"""
() => {
  const norm = s => (s || '').toLowerCase();
  const isHidden = el => {
    if (!el) return true;
    const t = (el.type || '').toLowerCase();
    if (t === 'hidden') return true;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') === 0) return true;
    if (el.offsetParent === null && st.position !== 'fixed') return true;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return true;
    // Off-screen honeypot traps (e.g. left:-9999px / top:-9999px) are visibility:
    // visible with a non-null offsetParent, so also reject boxes pushed off-screen.
    if (r.right < 1 || r.bottom < 1) return true;
    return false;
  };
  // 'kod' only as ALT text (a captcha label) — NOT as a src substring, which would
  // match unrelated images like /img/kodeks.png and mis-anchor the captcha field.
  const CAP_IMG = "img[src*='captcha' i], img[src*='securimage' i], img[alt*='captcha' i], img[alt*='kod' i], .captcha img";
  // Split camelCase and separators so "verifyCode"/"your_nick"/"web-site" match.
  const clean = s => (s || '').replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase().replace(/[_\-.]+/g, ' ');
  const forms = Array.from(document.querySelectorAll('form'));
  // Score each form by how many meaningful comment-form fields it has. On a page
  // with several forms (e.g. a shoutbox PLUS the guestbook, both with captchas)
  // this picks the richest form — the real guestbook — not just the first one
  // that happens to contain a captcha.
  const scoreForm = f => {
    let score = 0;
    for (const el of f.querySelectorAll('input, textarea, select')) {
      const t = (el.type || '').toLowerCase();
      if (t === 'hidden' || t === 'submit' || t === 'button') continue;
      const n = clean((el.name || '') + ' ' + (el.id || '') + ' ' + (el.placeholder || ''));
      if (t === 'email' || /\bmail\b|email/.test(n)) score += 3;
      if (t === 'url' || /\burl\b|website|webseite|homepage|www/.test(n)) score += 2;
      if (el.tagName === 'TEXTAREA' || /message|comment|kommentar|nachricht|\btext\b|body|content/.test(n)) score += 3;
      if (/\bname\b|autor|\bnick\b|author|vorname/.test(n)) score += 2;
      if (/captcha|verif|securit|\bkod\b/.test(n)) score += 1;
    }
    if (f.querySelector(CAP_IMG)) score += 1;
    return score;
  };
  let form = null, bestScore = 0;
  for (const f of forms) {
    const s = scoreForm(f);
    if (s > bestScore) { bestScore = s; form = f; }
  }
  if (!form) {
    form = forms.find(f => f.querySelector('textarea')) || (forms.length ? forms[forms.length - 1] : null);
  }
  const root = form || document;

  const result = { form_found: !!form };
  const used = new Set();
  const inputs = Array.from(root.querySelectorAll('input, textarea, select'));
  const key = el => el.name || el.id || '';
  const labelText = el => {
    let t = '';
    try {
      if (el.id) { const l = root.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) t += ' ' + norm(l.textContent); }
    } catch (e) {}
    const p = el.closest ? el.closest('label') : null;
    if (p) t += ' ' + norm(p.textContent);
    return t;
  };
  // `clean` (camelCase/separator splitter) is defined above with scoreForm.
  const toks = el => clean((el.name || '') + ' ' + (el.id || '') + ' ' + (el.placeholder || '') + ' ' +
                     ((el.getAttribute && el.getAttribute('autocomplete')) || '')) + ' ' + labelText(el);
  const pick = (role, test) => {
    if (result[role]) return;
    for (const el of inputs) {
      if (used.has(el) || !key(el)) continue;
      if (test(el)) { result[role] = key(el); used.add(el); return; }
    }
  };

  pick('email', el => (el.type || '').toLowerCase() === 'email');
  pick('email', el => !isHidden(el) && /mail|e-mail|email/.test(toks(el)));
  pick('url', el => (el.type || '').toLowerCase() === 'url');
  pick('url', el => !isHidden(el) && /\burl\b|website|webseite|homepage|\bwww\b|\bsite\b/.test(toks(el)));
  pick('message', el => el.tagName === 'TEXTAREA' && !isHidden(el));
  pick('message', el => !isHidden(el) && /message|comment|kommentar|nachricht|eintrag|tresc|beitrag|\btext\b|\bbody\b|content|otziv|feedback|сообщени|отзыв|коммент/.test(toks(el)));
  // Fallback: a hidden <textarea> is still the message target — WYSIWYG editors
  // (CKEditor/TinyMCE) hide the underlying textarea and show a rich-text widget,
  // which _fill_wysiwyg_editor handles. So accept a hidden textarea too.
  pick('message', el => el.tagName === 'TEXTAREA');

  // Name and city BEFORE captcha, so the geometric captcha fallback below can't
  // greedily consume a name/city input that sits near the captcha image.
  pick('name', el => !isHidden(el) && ['name','nickname'].includes(norm(el.getAttribute && el.getAttribute('autocomplete'))));
  pick('name', el => el.tagName === 'INPUT' && !isHidden(el) &&
       /\bname\b|autor|\bnick\b|author|vorname|username|\bfio\b|\bimya\b|absender|имя|фио|\bfrom\b/.test(toks(el)) &&
       !['email','url'].includes((el.type || '').toLowerCase()));
  pick('city', el => el.tagName === 'INPUT' && !isHidden(el) &&
       /\bcity\b|town|\bort\b|stadt|miasto|gorod|wohnort|город/.test(toks(el)));

  // captcha answer: keyword match first (most reliable), then fall back to the
  // visible text input physically nearest the captcha image.
  pick('captcha', el => el.tagName === 'INPUT' && !isHidden(el) &&
       /captcha|verif|securit|\bkod\b|\bcode\b/.test(toks(el)) &&
       !/\bzip\b|postal|pocztow|\bplz\b|discount|coupon|promo|voucher|\bpin\b|\barea\b|phone/.test(toks(el)) &&
       !['email','url'].includes((el.type || '').toLowerCase()));
  const capImg = root.querySelector(CAP_IMG);
  if (!result.captcha && capImg) {
    const ib = capImg.getBoundingClientRect();
    let best = null, bestD = 1e9;
    for (const el of inputs) {
      if (used.has(el) || !key(el) || el.tagName !== 'INPUT') continue;
      const t = (el.type || 'text').toLowerCase();
      if (['hidden','submit','button','checkbox','radio','email','url','file'].includes(t) || isHidden(el)) continue;
      const b = el.getBoundingClientRect();
      const d = Math.hypot((b.left+b.right)/2 - (ib.left+ib.right)/2, (b.top+b.bottom)/2 - (ib.top+ib.bottom)/2);
      if (d < bestD) { bestD = d; best = el; }
    }
    if (best) { result.captcha = key(best); used.add(best); }
  }
  pick('captcha_hash', el => (el.type || '').toLowerCase() === 'hidden' &&
       /captcha/.test(toks(el)) && /hash|secret|token|_id|hidden/.test(toks(el)));

  // submit button within the target form
  const btns = Array.from(root.querySelectorAll("input[type=submit], button[type=submit], button:not([type]), input[type=image], input[type=button]"));
  const neg = /reset|cancel|abbrechen|clear|l[oö]schen|logout|login|anmelden|search|suche|preview|vorschau/;
  const pos = /submit|send|senden|save|speichern|\badd\b|hinzu|\bpost\b|dodaj|eintragen|absenden|abschicken|publish|comment|kommentar|отправить/;
  for (const b of btns) {
    const t = norm(b.value) + ' ' + norm(b.textContent) + ' ' + norm(b.name) + ' ' + norm(b.id);
    if (neg.test(t)) continue;
    // A typeless <button> reflects .type==='submit', so relying on the property
    // would pick a WYSIWYG-toolbar button. Trust an explicit type=submit/image
    // ATTRIBUTE; for anything else require a positive submit-word match.
    const attrType = (b.getAttribute && (b.getAttribute('type') || '') || '').toLowerCase();
    const attrSubmit = (b.tagName === 'INPUT' && (attrType === 'submit' || attrType === 'image'))
                    || (b.tagName === 'BUTTON' && attrType === 'submit');
    if (attrSubmit || pos.test(t)) {
      result.submit_name = b.name || b.id || '';
      break;
    }
  }
  return result;
}
"""


async def _augment_config_from_dom(page, config):
    """For auto-detect (user/generic) sites, resolve empty field names from the
    live DOM. Built-in configs (auto_detect=False) are returned unchanged, so the
    hand-tuned 5 are never affected."""
    if not getattr(config, "auto_detect", False):
        return config
    try:
        detected = await page.evaluate(_FIELD_DETECT_JS)
    except Exception as e:
        logger.warning(f"Field auto-detect failed for {config.domain}: {e}")
        detected = {}

    updates: dict = {}

    def set_if_empty(attr, val):
        if val and not getattr(config, attr, ""):
            updates[attr] = val

    set_if_empty("name_field", detected.get("name"))
    set_if_empty("email_field", detected.get("email"))
    set_if_empty("url_field", detected.get("url"))
    set_if_empty("city_field", detected.get("city"))
    set_if_empty("message_field", detected.get("message"))
    set_if_empty("captcha_field", detected.get("captcha"))
    set_if_empty("captcha_hash_field", detected.get("captcha_hash"))

    submit_name = detected.get("submit_name")
    if submit_name and not config.submit_selector:
        updates["submit_selector"] = (
            f"input[name='{submit_name}'], button[name='{submit_name}'], "
            f"input[id='{submit_name}'], button[id='{submit_name}']"
        )

    # Safe fallbacks so form-scoping and message fill never break on odd pages.
    if not config.captcha_field and not updates.get("captcha_field"):
        updates["captcha_field"] = "captcha"
    if not config.message_field and not updates.get("message_field"):
        updates["message_field"] = "message"

    new_config = replace(config, **updates) if updates else config
    logger.info(
        f"Auto-detected fields for {new_config.domain}: "
        f"name={new_config.name_field!r} email={new_config.email_field!r} "
        f"url={new_config.url_field!r} msg={new_config.message_field!r} "
        f"captcha={new_config.captcha_field!r} submit={bool(new_config.submit_selector)} "
        f"(form_found={detected.get('form_found')})"
    )
    return new_config


async def _detect_site_fields(url: str) -> dict:
    """Load a URL headless, run the field auto-detector, and return the proposed
    mapping + a screenshot so the user can confirm before saving the site."""
    from playwright.async_api import async_playwright
    from src.site_configs import normalize_domain

    result = {
        "url": url, "form_found": False, "captcha_found": False,
        "fields": {}, "screenshot": "", "warnings": [],
    }
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        )
        await _attach_ssrf_guard(ctx)
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as nav_err:
            raise RuntimeError(f"navigation failed: {str(nav_err)[:80]}")
        await asyncio.sleep(1.5)
        try:
            await _human_scroll(page)
        except Exception:
            pass

        detected = await page.evaluate(_FIELD_DETECT_JS)
        result["form_found"] = bool(detected.get("form_found"))
        result["fields"] = {
            "name": detected.get("name", ""),
            "email": detected.get("email", ""),
            "url": detected.get("url", ""),
            "city": detected.get("city", ""),
            "message": detected.get("message", ""),
            "captcha": detected.get("captcha", ""),
            "captcha_hash": detected.get("captcha_hash", ""),
            "submit": detected.get("submit_name", ""),
        }
        result["captcha_found"] = await page.evaluate(
            "() => !!document.querySelector(\"img[src*='captcha' i], .captcha img, img[alt*='captcha' i], img[src*='securimage' i]\")"
        )

        if not result["form_found"]:
            result["warnings"].append("No form was found on this page — check the URL points to the form itself.")
        if not result["fields"]["message"]:
            result["warnings"].append("No message/comment field detected — posting may not work here.")
        if not result["captcha_found"]:
            result["warnings"].append("No captcha image detected (that's fine if the site has none).")

        try:
            Path("screenshots").mkdir(exist_ok=True)
            sp = f"screenshots/detect_{normalize_domain(url)}_{int(time.time())}.png"
            await page.screenshot(path=sp, full_page=False)
            result["screenshot"] = f"/screenshots/{Path(sp).name}"
        except Exception:
            pass

        await browser.close()
    finally:
        try:
            await pw.stop()
        except Exception:
            pass
    return result


async def _fill_form_humanized(page, config, profile, captcha_token, backlinks, skip_captcha=False):
    """Fill form fields with human-like behavior.

    `backlinks` is a list of client URLs (a bare string is also accepted for
    backwards-compat). ALL of them are listed one-per-line in the comment body;
    the first URL also fills the site's single homepage/url input field.
    """
    import random as _random

    # Find the correct form — the one containing the captcha field
    if config.captcha_hash_field:
        form_scope = page.locator(
            f"form:has(input[name='{config.captcha_field}']), "
            f"form:has(input[name='{config.captcha_hash_field}'])"
        ).last  # last form on page is usually the guestbook, not login
    else:
        form_scope = page.locator(
            f"form:has(input[name='{config.captcha_field}'])"
        ).last
    use_form = await form_scope.count() > 0
    if use_form:
        logger.info(f"Targeting form with captcha field '{config.captcha_field}'")

    def locate(selector):
        # IMPORTANT: only use name-based selectors, never #id which can match IMG elements
        if use_form:
            return form_scope.locator(selector).first
        return page.locator(selector).first

    message_text, primary_link, backlinks = _compose_backlink_content(profile["comment"], backlinks)

    field_map = [
        (config.name_field, profile["name"], "name"),
        (config.email_field, profile["email"], "email"),
        (config.url_field, primary_link, "url"),
        (config.city_field, profile.get("city", ""), "city"),
        (config.message_field, message_text, "message"),
        (config.captcha_hash_field, "", "captcha_hash"),
        (config.captcha_field, captcha_token, "captcha"),
    ]

    for field_name, value, label in field_map:
        if not field_name:
            continue
        if label == "captcha_hash":
            continue
        if skip_captcha and label == "captcha":
            continue
        try:
            el = locate(f"input[name='{field_name}'], textarea[name='{field_name}']")
            if await el.count() == 0:
                logger.warning(f"Field '{field_name}' NOT FOUND on page")
                if label == "message":
                    await _fill_wysiwyg_editor(page, value, field_name)
                continue

            is_visible = await el.is_visible()

            if label == "message":
                if is_visible:
                    await el.click(delay=_random.randint(100, 300))
                    await _human_delay(200, 500)
                    await el.fill(value, timeout=5000)
                    await _human_delay(300, 700)
                    logger.info(f"Filled message field '{field_name}'")
                else:
                    logger.info(f"Message textarea '{field_name}' hidden (WYSIWYG editor)")
                await _fill_wysiwyg_editor(page, value, field_name)
                continue

            if not is_visible:
                logger.info(f"Skipping hidden field '{field_name}'")
                continue

            await el.click(delay=_random.randint(100, 300))
            await _human_delay(200, 600)
            if value:
                await el.fill(value, timeout=5000)
            if label == "captcha":
                actual = await el.input_value()
                logger.info(f"CAPTCHA filled: '{actual}' (expected '{value}')")
                if not actual or actual != value:
                    logger.error(f"CAPTCHA MISMATCH: expected '{value}' got '{actual}'")
                    await el.fill(value, timeout=5000)
            else:
                logger.info(f"Filled {label} field '{field_name}'")
        except Exception as e:
            logger.error(f"Fill FAILED for '{field_name}': {e}")
            if label == "message":
                await _fill_wysiwyg_editor(page, value, field_name)


    # Handle star rating if configured
    if config.rating_selector and config.rating_value:
        try:
            rating_set = await page.evaluate("""(rating) => {
                // Method 1: Hidden/text input for rating (Krajee Bootstrap Star Rating)
                const allInputs = document.querySelectorAll('input');
                for (const inp of allInputs) {
                    const name = (inp.name || '').toLowerCase();
                    const id = (inp.id || '').toLowerCase();
                    if (inp.type === 'radio') continue;
                    if (name.includes('rating') || name.includes('score') || id.includes('rating') || id.includes('star-rating')) {
                        inp.value = String(rating);
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        // Update Krajee visual: set filled-stars width
                        const container = inp.closest('.form-group') || inp.parentElement;
                        if (container) {
                            const filledStars = container.querySelector('.filled-stars');
                            if (filledStars) filledStars.style.width = (rating * 20) + '%';
                        }
                        // Try jQuery plugin update
                        if (typeof jQuery !== 'undefined') {
                            try { jQuery('#' + (inp.id || '')).rating('update', rating); } catch(e) {}
                        }
                        return 'hidden_input:' + inp.name + '=' + rating;
                    }
                }

                // Method 2: Radio input groups with numeric values
                const allRadios = document.querySelectorAll('input[type="radio"]');
                const groups = {};
                for (const r of allRadios) {
                    const name = r.name || '';
                    if (!name) continue;
                    if (!groups[name]) groups[name] = [];
                    groups[name].push(r);
                }
                for (const [name, inputs] of Object.entries(groups)) {
                    const values = inputs.map(r => parseInt(r.value)).filter(v => !isNaN(v));
                    if (values.length >= 3 && values.includes(rating)) {
                        for (const r of inputs) {
                            if (parseInt(r.value) === rating) {
                                r.checked = true;
                                r.click();
                                r.dispatchEvent(new Event('change', {bubbles: true}));
                                const lbl = r.id ? document.querySelector('label[for="' + r.id + '"]') : null;
                                if (lbl) lbl.click();
                                return 'radio:' + name + '=' + r.value;
                            }
                        }
                    }
                }

                // Method 3: data-rating or data-value attributes
                const dataEl = document.querySelector('[data-rating="' + rating + '"], [data-value="' + rating + '"]');
                if (dataEl) {
                    dataEl.click();
                    return 'data_attr';
                }

                return null;
            }""", config.rating_value)

            if rating_set:
                logger.info(f"Set rating {config.rating_value} via JS: {rating_set}")
            else:
                # Last resort: CSS selector with reverse-order awareness
                stars = page.locator(config.rating_selector)
                star_count = await stars.count()
                if star_count > 0:
                    target_idx = min(config.rating_value - 1, star_count - 1)
                    await stars.nth(target_idx).click()
                    logger.info(f"Clicked star rating via CSS at index {target_idx}/{star_count}")
        except Exception as e:
            logger.warning(f"Rating fill failed: {e}")

    # Fill remaining empty VISIBLE fields. Honeypots (bot-traps) are almost always
    # CSS-hidden, so excluding invisible inputs here is the main defense against
    # tripping them on an unknown site; a name blocklist is the backup.
    try:
        remaining = await (form_scope if use_form else page).evaluate("""(el) => {
            const root = el || document;
            const fields = [];
            root.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=checkbox]):not([type=radio]):not([type=email]):not([type=url]):not([type=file]):not([type=password])').forEach(inp => {
                if (inp.value) return;
                const st = window.getComputedStyle(inp);
                if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') === 0) return;
                if (inp.offsetParent === null && st.position !== 'fixed') return;
                const r = inp.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) return;
                fields.push(inp.name || inp.id || '');
            });
            return fields.filter(f => f);
        }""")
        skip = {config.name_field, config.email_field, config.url_field,
                config.city_field, config.message_field, config.captcha_field,
                config.captcha_hash_field}
        skip.update(config.honeypot_fields or [])
        skip.update(config.skip_fields or [])
        # Generic honeypot / do-not-fill names — skipped even with no per-site config.
        _HONEYPOT = {"bot", "honeypot", "hp", "spam", "trap", "nofill", "no_fill",
                     "dontfill", "leaveblank", "leave_blank", "winnie"}
        for field_name in remaining:
            if field_name in skip:
                continue
            if field_name.lower() in _HONEYPOT:
                logger.info(f"Skipping honeypot-like field '{field_name}'")
                continue
            try:
                el = locate(f"input[name='{field_name}'], textarea[name='{field_name}']")
                if await el.count() > 0:
                    await el.fill(profile.get("city", "Anonymous"), timeout=3000)
                    logger.info(f"Filled extra field '{field_name}'")
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Extra field scan failed: {e}")

    if config.select_fields:
        for field_name, options in config.select_fields.items():
            try:
                sel = locate(f"select[name='{field_name}']")
                if await sel.count() > 0 and options:
                    await sel.select_option(options[0], timeout=3000)
            except Exception:
                pass


async def _click_submit_humanized(page, config) -> bool:
    """Click submit with human-like movement. Returns True if click succeeded."""
    try:
        if config.submit_selector:
            btn = page.locator(config.submit_selector).first
        else:
            btn = page.locator("input[type='submit'], button[type='submit'], input[type='image']").first

        if await btn.count() > 0:
            await btn.scroll_into_view_if_needed()
            box = await btn.bounding_box()
            if box:
                x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
                y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                await page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.1, 0.3))
                await page.mouse.click(x, y, delay=random.randint(50, 150))
                logger.info("Submit button clicked successfully")
                return True
            else:
                logger.warning("Submit button found but bounding box is None (not visible?)")
                return False
        else:
            logger.warning("Submit button NOT FOUND on page")
            return False
    except Exception as e:
        logger.error(f"Submit click failed: {e}")
        return False


async def _fill_wysiwyg_editor(page, text: str, field_name: str = ""):
    """Fill WYSIWYG rich text editors (CKEditor, TinyMCE, Quill, contenteditable),
    then force the underlying <textarea> to hold the value so server-side
    validation (which reads the textarea) doesn't see an empty field."""
    html_text = text.replace("\n", "<br>")

    # Try CKEditor — setData then updateElement() so the underlying <textarea>
    # actually receives the content (a raw submit-button click bypasses
    # CKEditor's own form-sync, otherwise the field submits empty).
    try:
        await page.evaluate(f"""() => {{
            if (typeof CKEDITOR !== 'undefined') {{
                for (const key in CKEDITOR.instances) {{
                    CKEDITOR.instances[key].setData({json.dumps(html_text)});
                    try {{ CKEDITOR.instances[key].updateElement(); }} catch(e) {{}}
                }}
            }}
        }}""")
        logger.info("WYSIWYG: CKEditor content set + synced")
    except Exception:
        pass

    # Try CKEditor 5 — its instance is attached to the editable DOM element as
    # `.ckeditorInstance`. CKEditor 5 ignores direct innerHTML edits (it syncs the
    # source <textarea> from its own model), so we must call setData() on the
    # instance, then updateSourceElement() to write it into the textarea. This is
    # the editor used by the Ilch CMS guestbook (ersterzug / geini).
    try:
        n5 = await page.evaluate(f"""() => {{
            let n = 0;
            document.querySelectorAll('.ck-editor__editable, .ck-content').forEach((el) => {{
                try {{
                    const ed = el.ckeditorInstance;
                    if (ed && typeof ed.setData === 'function') {{
                        ed.setData({json.dumps(html_text)});
                        if (typeof ed.updateSourceElement === 'function') ed.updateSourceElement();
                        n++;
                    }}
                }} catch (e) {{}}
            }});
            return n;
        }}""")
        if n5:
            logger.info(f"WYSIWYG: CKEditor 5 content set on {n5} editor(s)")
    except Exception:
        pass

    # Try TinyMCE (all editors, v4/v5/v6) — setContent then save()/triggerSave()
    # to push the content back into the hidden <textarea> before submit.
    try:
        await page.evaluate(f"""() => {{
            const tm = window.tinymce || window.tinyMCE;
            if (tm) {{
                const eds = (tm.editors && tm.editors.length) ? tm.editors
                          : (tm.activeEditor ? [tm.activeEditor] : []);
                for (const ed of eds) {{
                    try {{ ed.setContent({json.dumps(html_text)}); ed.save(); }} catch(e) {{}}
                }}
                try {{ if (typeof tm.triggerSave === 'function') tm.triggerSave(); }} catch(e) {{}}
            }}
        }}""")
        logger.info("WYSIWYG: TinyMCE content set + saved")
    except Exception:
        pass

    # Try contenteditable iframes (common in CKEditor/TinyMCE)
    try:
        frames = page.frame_locator("iframe")
        body_el = frames.locator("body[contenteditable='true']").first
        # Check if we can find a contenteditable body in any iframe
        count = await page.evaluate("""() => {
            const iframes = document.querySelectorAll('iframe');
            for (const iframe of iframes) {
                try {
                    const body = iframe.contentDocument.body;
                    if (body && body.contentEditable === 'true') {
                        return true;
                    }
                } catch(e) {}
            }
            return false;
        }""")
        if count:
            await page.evaluate(f"""() => {{
                const iframes = document.querySelectorAll('iframe');
                for (const iframe of iframes) {{
                    try {{
                        const body = iframe.contentDocument.body;
                        if (body && body.contentEditable === 'true') {{
                            body.innerHTML = {json.dumps(html_text)};
                        }}
                    }} catch(e) {{}}
                }}
            }}""")
            logger.info("WYSIWYG: contenteditable iframe filled")
    except Exception:
        pass

    # Try Quill editor
    try:
        await page.evaluate(f"""() => {{
            const qlEditor = document.querySelector('.ql-editor');
            if (qlEditor) {{
                qlEditor.textContent = {json.dumps(text)};
            }}
        }}""")
    except Exception:
        pass

    # Try contenteditable divs on the page itself
    try:
        await page.evaluate(f"""() => {{
            const editables = document.querySelectorAll('[contenteditable="true"]:not(body)');
            for (const el of editables) {{
                if (el.offsetHeight > 50) {{
                    el.innerHTML = {json.dumps(html_text)};
                    break;
                }}
            }}
        }}""")
    except Exception:
        pass

    # Most robust fallback: many editors (e.g. the Ilch CMS guestbook used by
    # ersterzug/geini) only copy their content into the hidden <textarea> when the
    # editor fires its own sync — a raw submit-button click can bypass that and
    # submit an empty field ("Text must be filled in"). So we (1) fire the events
    # editors listen on to trigger their sync, then (2) set the textarea value
    # directly as the final authority.
    try:
        await page.evaluate(
            """(args) => {
                const name = args[0], txt = args[1];
                const fire = (el) => {
                    for (const t of ['input', 'change', 'keyup', 'blur']) {
                        try { el.dispatchEvent(new Event(t, {bubbles: true})); } catch(e) {}
                    }
                };
                // 1) Trigger editor -> textarea sync from the (already filled) editors.
                document.querySelectorAll('[contenteditable="true"]').forEach(fire);
                document.querySelectorAll('iframe').forEach((f) => {
                    try {
                        const b = f.contentDocument && f.contentDocument.body;
                        if (b && b.contentEditable === 'true') fire(b);
                    } catch (e) {}
                });
                // 2) Set the underlying textarea(s) directly and fire their events.
                let tas = [];
                if (name) tas = Array.from(document.querySelectorAll('textarea[name="' + name + '"]'));
                if (!tas.length) {
                    tas = Array.from(document.querySelectorAll('textarea')).filter((t) =>
                        /text|message|comment|body|content|nachricht|kommentar|tresc|beitrag/i
                            .test((t.name || '') + ' ' + (t.id || '')));
                }
                if (!tas.length) tas = Array.from(document.querySelectorAll('textarea'));
                for (const ta of tas) { ta.value = txt; fire(ta); }
                return tas.length;
            }""",
            [field_name, text],
        )
        logger.info(f"WYSIWYG: textarea '{field_name or 'auto'}' set directly + events fired")
    except Exception:
        pass


async def _human_scroll(page):
    """Perform human-like scrolling."""
    for _ in range(random.randint(1, 3)):
        scroll_y = random.randint(200, 600)
        await page.evaluate(f"window.scrollBy(0, {scroll_y})")
        await asyncio.sleep(random.uniform(0.3, 1.0))


async def _human_delay(min_ms: int, max_ms: int):
    """Random delay between min and max milliseconds."""
    base = random.randint(min_ms, max_ms)
    base += abs(random.gauss(0, 100))
    await asyncio.sleep(base / 1000.0)


# Mount screenshots directory
SCREENSHOTS_DIR = Path("screenshots")
SCREENSHOTS_DIR.mkdir(exist_ok=True)

# Restore the last batch's results + full run history so a restart keeps them.
_load_results()
_load_run_history()

# Load any user-added sites from disk so they merge with the built-in 5.
try:
    from src.site_configs import load_user_sites
    load_user_sites()
except Exception as _e:
    logger.warning(f"Could not load user sites: {_e}")


@app.get("/screenshots/{filename}")
async def get_screenshot(filename: str):
    # Path-traversal safe: reject separators/parent refs and confirm the resolved
    # path is a real file sitting directly inside SCREENSHOTS_DIR.
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(404)
    base = SCREENSHOTS_DIR.resolve()
    path = (base / filename).resolve()
    if path.parent != base or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


def run_server(host: str | None = None, port: int = 8000):
    # Bind to localhost by default so the tool is NOT exposed on the LAN (it has
    # no authentication). Advanced users can opt into a wider bind via env var.
    host = host or os.environ.get("BACKLINK_HOST", "127.0.0.1")
    port = int(os.environ.get("BACKLINK_PORT", os.environ.get("PORT", port)))
    logger.info(f"Starting Backlink Generator at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
