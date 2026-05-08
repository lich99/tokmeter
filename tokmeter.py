#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["polars>=1.0"]
# ///
"""tokmeter — local dashboard for Claude Code & Codex CLI token cost.

Reads ~/.claude/projects/**/*.jsonl and ~/.codex/sessions/**/*.jsonl,
aggregates with polars, serves a single-file HTML on localhost.

Usage:
  uvx tokmeter            # one-shot run from PyPI
  uv run tokmeter.py      # single-file mode (PEP 723)
  python3 tokmeter.py     # if polars already installed
"""
import argparse
import bisect
import glob
import http.server
import json
import os
import re
import socketserver
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from urllib.parse import parse_qs, urlparse

import sqlite3
import polars as pl

ROOT = os.path.expanduser("~/.claude/projects")
CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions")
CODEX_LOGS_DB = os.path.expanduser("~/.codex/logs_2.sqlite")
HOME = os.path.expanduser("~")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_HTML = os.path.join(SCRIPT_DIR, "dashboard.html")
DEFAULT_PORT = 8765


def get_html():
    with open(DASHBOARD_HTML, encoding="utf-8") as f:
        return f.read()


@lru_cache(maxsize=8192)
def _find_dir_match(base: str, candidate: str):
    """Find a subdirectory of `base` matching `candidate`, treating '-' and '_'
    as equivalent. Claude Code's project encoding replaces both `/` and `_` with `-`,
    so when reversing we accept either literal-dash or underscore-canonical forms."""
    if not os.path.isdir(base):
        return None
    full_direct = os.path.join(base, candidate)
    if os.path.isdir(full_direct):
        return candidate
    target = candidate.replace("-", "_")
    try:
        entries = os.listdir(base)
    except OSError:
        return None
    for entry in entries:
        if entry.replace("-", "_") == target and os.path.isdir(os.path.join(base, entry)):
            return entry
    return None


@lru_cache(maxsize=2048)
def decode_project_name(encoded: str) -> str:
    """`-Users-nek0-Code-Claw-proxy` → `~/Code/Claw_proxy` by greedy filesystem match.

    Claude Code encodes project paths as `<path>` with both `/` and `_` collapsed to `-`.
    Decoding is ambiguous in general, so at each position we take the longest segment
    that resolves to an existing directory (treating `-`/`_` as equivalent), falling
    back to single-part literal when nothing matches."""
    if not encoded or not encoded.startswith("-"):
        return encoded
    parts = encoded[1:].split("-")
    if not parts:
        return encoded
    path = "/"
    i = 0
    n = len(parts)
    MAX_JOIN = 16
    while i < n:
        matched_len = 0
        matched_dir = None
        for j in range(min(MAX_JOIN, n - i), 0, -1):
            candidate = "-".join(parts[i:i + j])
            actual = _find_dir_match(path, candidate)
            if actual is not None:
                matched_len = j
                matched_dir = actual
                break
        if matched_len == 0:
            matched_len = 1
            matched_dir = parts[i]
        path = os.path.join(path, matched_dir)
        i += matched_len
    if path == HOME:
        return "~"
    if path.startswith(HOME + "/"):
        return "~/" + path[len(HOME) + 1:]
    return path

PRICE = {
    "claude-opus-4-7":   {"in":5.00,"out":25.00,"cw5m":6.25,"cw1h":10.00,"cr":0.50},
    "claude-opus-4-6":   {"in":5.00,"out":25.00,"cw5m":6.25,"cw1h":10.00,"cr":0.50},
    "claude-opus-4-5":   {"in":5.00,"out":25.00,"cw5m":6.25,"cw1h":10.00,"cr":0.50},
    "claude-opus-4-1":   {"in":15.00,"out":75.00,"cw5m":18.75,"cw1h":30.00,"cr":1.50},
    "claude-opus-4":     {"in":15.00,"out":75.00,"cw5m":18.75,"cw1h":30.00,"cr":1.50},
    "claude-sonnet-4-6": {"in":3.00,"out":15.00,"cw5m":3.75,"cw1h":6.00,"cr":0.30},
    "claude-sonnet-4-5": {"in":3.00,"out":15.00,"cw5m":3.75,"cw1h":6.00,"cr":0.30},
    "claude-sonnet-4":   {"in":3.00,"out":15.00,"cw5m":3.75,"cw1h":6.00,"cr":0.30},
    "claude-haiku-4-5":  {"in":1.00,"out":5.00,"cw5m":1.25,"cw1h":2.00,"cr":0.10},
    "claude-haiku-3-5":  {"in":0.80,"out":4.00,"cw5m":1.00,"cw1h":1.60,"cr":0.08},
}

def normalize_model(m: str) -> str:
    m = (m or "unknown").lower()
    for suf in ("[1m]","-20251001","-20250929","-20250522"):
        if m.endswith(suf):
            m = m[:-len(suf)]
    return m

def price_for(model):
    if model in PRICE: return PRICE[model]
    for k, v in PRICE.items():
        if model.startswith(k): return v
    return None

def cost_of(model, in_t, out_t, cw5, cw1, cr):
    p = price_for(model)
    if not p: return 0.0
    return (in_t*p["in"] + out_t*p["out"] + cw5*p["cw5m"] + cw1*p["cw1h"] + cr*p["cr"]) / 1_000_000

# ---------- in-memory cache, refreshed by file mtime/size ----------
_lock = threading.Lock()
# path -> { mtime, size, records: [ (msg_id, ts_ms, model, project, is_sub, in, out, cw5m, cw1h, cr) ] }
_files = {}
_last_full_refresh = 0.0
# Sorted/dedup cache rebuilt only when files change — avoids re-sorting 50k+ rows per request.
_sorted_cache = None     # list of records, ascending by ts
_sorted_ts_cache = None  # parallel list of ts (ms) for bisect

def _cc_price(model: str):
    """Look up Claude Code price by prefix. Returns dict or None."""
    m = model.lower()
    for p in PRICE_LIST:
        if m.startswith(p["prefix"]):
            return p
    return None

# Record schema (22 columns) shared by Claude Code and Codex parsers:
RECORD_SCHEMA = [
    ("msg_id",   pl.Utf8),  ("ts",       pl.Int64),
    ("model",    pl.Utf8),  ("project",  pl.Utf8),
    ("is_sub",   pl.Int8),  ("source",   pl.Utf8),     # 'cc' | 'codex'
    ("in_t",     pl.Int64), ("cached_t", pl.Int64),
    ("out_t",    pl.Int64), ("reason_t", pl.Int64),
    ("cw5m",     pl.Int64), ("cw1h",     pl.Int64),  ("cr",  pl.Int64),
    ("tier",     pl.Utf8),  ("long_ctx", pl.Int8),
    # Pre-computed per-row cost components:
    ("cost_in",          pl.Float64),
    ("cost_cached",      pl.Float64),
    ("cost_out_text",    pl.Float64),
    ("cost_out_reason",  pl.Float64),
    ("cost_cw5m",        pl.Float64),
    ("cost_cw1h",        pl.Float64),
    ("cost_cr",          pl.Float64),
]

def parse_file(path):
    """Claude Code parser. Emits records in RECORD_SCHEMA shape."""
    rel = os.path.relpath(path, ROOT)
    raw_project = rel.split(os.sep)[0]
    project = decode_project_name(raw_project)
    is_sub = (os.sep + "subagents" + os.sep) in path
    out = []
    try:
        with open(path, errors="replace") as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") != "assistant":
                    continue
                ts_s = rec.get("timestamp")
                if not ts_s:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_s.replace("Z", "+00:00"))
                except Exception:
                    continue
                msg = rec.get("message") or {}
                u = msg.get("usage") or {}
                if not u:
                    continue
                mid = msg.get("id") or ""
                model = normalize_model(msg.get("model"))
                if model == "<synthetic>" or not model or model == "unknown":
                    continue
                in_t = int(u.get("input_tokens") or 0)
                out_t = int(u.get("output_tokens") or 0)
                cr_t = int(u.get("cache_read_input_tokens") or 0)
                cw_total = int(u.get("cache_creation_input_tokens") or 0)
                cw_b = u.get("cache_creation") or {}
                cw5 = int(cw_b.get("ephemeral_5m_input_tokens") or 0)
                cw1 = int(cw_b.get("ephemeral_1h_input_tokens") or 0)
                if cw5 + cw1 == 0 and cw_total > 0:
                    cw5 = cw_total
                p = _cc_price(model)
                if p:
                    c_in   = in_t  * p["in"]   / 1_000_000
                    c_out  = out_t * p["out"]  / 1_000_000
                    c_cw5  = cw5   * p["cw5m"] / 1_000_000
                    c_cw1  = cw1   * p["cw1h"] / 1_000_000
                    c_cr   = cr_t  * p["cr"]   / 1_000_000
                else:
                    c_in = c_out = c_cw5 = c_cw1 = c_cr = 0.0
                out.append((
                    mid, int(ts.timestamp() * 1000),
                    model, project,
                    1 if is_sub else 0, "cc",
                    in_t, 0, out_t, 0,
                    cw5, cw1, cr_t,
                    "standard", 0,
                    c_in, 0.0, c_out, 0.0,
                    c_cw5, c_cw1, c_cr,
                ))
    except Exception:
        pass
    return out


def parse_codex_rollout(path):
    """Codex rollout (~/.codex/sessions/...) parser. One record per token_count
    event. Tier is looked up per-event from the sqlite-backed tier map.
    Subagent sessions are flagged via session_meta.payload.agent_role / agent_nickname."""
    out = []
    session_id = None
    project = None
    current_model = None
    is_sub_session = False
    try:
        with open(path, errors="replace") as f:
            for line in f:
                if '"timestamp"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                t = rec.get("type")
                payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
                if t == "session_meta":
                    session_id = payload.get("id")
                    cwd = payload.get("cwd") or ""
                    if cwd == HOME:
                        project = "~"
                    elif cwd.startswith(HOME + "/"):
                        project = "~/" + cwd[len(HOME) + 1:]
                    else:
                        project = cwd or "(unknown)"
                    # Subagent sessions carry an explicit agent role/nickname.
                    is_sub_session = bool(payload.get("agent_role") or payload.get("agent_nickname"))
                elif t == "turn_context":
                    m = payload.get("model")
                    if m:
                        current_model = m.lower()
                elif t == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info")
                    if not info:
                        continue
                    last = info.get("last_token_usage") or {}
                    in_t     = int(last.get("input_tokens") or 0)
                    cached_t = int(last.get("cached_input_tokens") or 0)
                    out_t    = int(last.get("output_tokens") or 0)
                    reason_t = int(last.get("reasoning_output_tokens") or 0)
                    # Skip context-size snapshot events (all zero usage fields)
                    if in_t == 0 and out_t == 0 and cached_t == 0:
                        continue
                    ts_s = rec.get("timestamp")
                    if not ts_s:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_s.replace("Z", "+00:00"))
                    except Exception:
                        continue
                    ts_ms = int(ts.timestamp() * 1000)
                    tier = lookup_tier_for(session_id, ts_ms) if session_id else "standard"
                    p = lookup_oai_price(current_model or "")
                    long_ctx = 1 if (p and ("in_long" in p) and in_t > LONG_CTX_THRESHOLD) else 0
                    cc = codex_cost_components(current_model or "", in_t, cached_t, out_t, reason_t, tier)
                    out.append((
                        "",  # msg_id unused for codex
                        ts_ms,
                        current_model or "unknown",
                        project or "(unknown)",
                        1 if is_sub_session else 0, "codex",
                        in_t, cached_t, out_t, reason_t,
                        0, 0, 0,
                        tier, long_ctx,
                        cc["cost_in"], cc["cost_cached"], cc["cost_out_text"], cc["cost_out_reason"],
                        0.0, 0.0, 0.0,
                    ))
    except Exception:
        pass
    return out

def _scan_dir(root_glob: str, parser, container: dict, force: bool):
    """Generic mtime+size based incremental rescanner. Returns (n_scanned, n_changed, n_removed)."""
    paths = glob.glob(root_glob, recursive=True)
    changed = []
    seen = set()
    for path in paths:
        seen.add(path)
        try:
            st = os.stat(path)
        except OSError:
            continue
        prev = container.get(path)
        if force or prev is None or prev["mtime"] != st.st_mtime or prev["size"] != st.st_size:
            changed.append((path, st.st_mtime, st.st_size))
    removed = [p for p in container if p not in seen]
    with _lock:
        for p in removed:
            del container[p]
        for path, mt, sz in changed:
            recs = parser(path)
            container[path] = {"mtime": mt, "size": sz, "records": recs}
    return len(paths), len(changed), len(removed)

# Codex rollouts get their own bucket (parallel to _files for Claude Code).
_codex_files = {}

def refresh(force=False):
    """Re-parse only files whose mtime/size changed since last scan, for both
    Claude Code (~/.claude/projects) and Codex (~/.codex/sessions)."""
    global _last_full_refresh
    cc_n, cc_changed, cc_removed = _scan_dir(
        os.path.join(ROOT, "**", "*.jsonl"), parse_file, _files, force)

    # When Codex sqlite tier-map changes, must invalidate cached tier lookups
    # AND re-parse all codex rollouts (their per-row tier was baked at parse time).
    tier_invalidated = False
    if os.path.exists(CODEX_LOGS_DB):
        try:
            db_mtime = os.stat(CODEX_LOGS_DB).st_mtime
        except OSError:
            db_mtime = None
        if db_mtime is not None and getattr(refresh, "_last_db_mtime", None) != db_mtime:
            invalidate_codex_tier_map()
            refresh._last_db_mtime = db_mtime
            tier_invalidated = True

    cx_n, cx_changed, cx_removed = _scan_dir(
        os.path.join(CODEX_SESSIONS, "**", "*.jsonl"),
        parse_codex_rollout, _codex_files,
        force or tier_invalidated)

    with _lock:
        _last_full_refresh = time.time()
    if cc_changed or cc_removed or cx_changed or cx_removed or tier_invalidated:
        invalidate_df()
    return {
        "scanned":  cc_n + cx_n,
        "changed":  cc_changed + cx_changed,
        "removed":  cc_removed + cx_removed,
        "cc":     {"scanned": cc_n, "changed": cc_changed, "removed": cc_removed},
        "codex":  {"scanned": cx_n, "changed": cx_changed, "removed": cx_removed},
    }

def all_records():
    """Materialize deduped record list (sorted ascending by ts)."""
    seen = set()
    out = []
    with _lock:
        for entry in _files.values():
            for r in entry["records"]:
                mid = r[0]
                if mid:
                    if mid in seen:
                        continue
                    seen.add(mid)
                out.append(r)
    out.sort(key=lambda r: r[1])
    return out

# ---------- aggregation for /api/aggregate (polars-vectorized) ----------
def tz_offset_ms(tz):
    if tz == "UTC": return 0
    if tz == "ET": return -4 * 3600 * 1000  # EDT
    return 0

# Bucket sizes (s).
SECONDS_IN_BUCKET = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "hour": 3600,
    "6h": 21600,
    "1d": 86400, "day": 86400,
}

# Pricing: list of prefix → multipliers. Order matters — longer prefixes first
# so `claude-opus-4-7` matches before `claude-opus-4`.
PRICE_LIST = [
    {"prefix": "claude-opus-4-7",   "in": 5.00,  "out": 25.00, "cw5m": 6.25,  "cw1h": 10.00, "cr": 0.50},
    {"prefix": "claude-opus-4-6",   "in": 5.00,  "out": 25.00, "cw5m": 6.25,  "cw1h": 10.00, "cr": 0.50},
    {"prefix": "claude-opus-4-5",   "in": 5.00,  "out": 25.00, "cw5m": 6.25,  "cw1h": 10.00, "cr": 0.50},
    {"prefix": "claude-opus-4-1",   "in": 15.00, "out": 75.00, "cw5m": 18.75, "cw1h": 30.00, "cr": 1.50},
    {"prefix": "claude-opus-4",     "in": 15.00, "out": 75.00, "cw5m": 18.75, "cw1h": 30.00, "cr": 1.50},
    {"prefix": "claude-sonnet-4-6", "in": 3.00,  "out": 15.00, "cw5m": 3.75,  "cw1h": 6.00,  "cr": 0.30},
    {"prefix": "claude-sonnet-4-5", "in": 3.00,  "out": 15.00, "cw5m": 3.75,  "cw1h": 6.00,  "cr": 0.30},
    {"prefix": "claude-sonnet-4",   "in": 3.00,  "out": 15.00, "cw5m": 3.75,  "cw1h": 6.00,  "cr": 0.30},
    {"prefix": "claude-haiku-4-5",  "in": 1.00,  "out": 5.00,  "cw5m": 1.25,  "cw1h": 2.00,  "cr": 0.10},
    {"prefix": "claude-haiku-3-5",  "in": 0.80,  "out": 4.00,  "cw5m": 1.00,  "cw1h": 1.60,  "cr": 0.08},
]

# ─── OpenAI / Codex pricing ──────────────────────────────────────────────────
# Per 1M tokens, $USD. Values:
#   in        : standard input rate (new tokens, not cached)
#   cached    : cached input rate (~10% of input)
#   out       : output rate (covers both text + reasoning tokens)
#   in_long   : >272K input rate (only for models with the long-context tier)
#   cached_long, out_long: same idea
#   priority  : multiplier for priority service tier (Codex "Fast mode")
# Order: longest prefix first so str.starts_with picks the most specific.
PRICE_LIST_OAI = [
    {"prefix":"gpt-5.5-pro",     "in":30.00,"cached":0.0,  "out":180.00, "priority":2.5},
    {"prefix":"gpt-5.5",         "in": 5.00,"cached":0.50,"out": 30.00,
        "in_long":10.00,"cached_long":1.00,"out_long":45.00, "priority":2.5},
    {"prefix":"gpt-5.4-pro",     "in":30.00,"cached":0.0,  "out":180.00,
        "in_long":60.00,"cached_long":0.0,"out_long":270.00, "priority":2.0},
    {"prefix":"gpt-5.4-mini",    "in":0.75,"cached":0.075,"out":4.50,    "priority":2.0},
    {"prefix":"gpt-5.4-nano",    "in":0.20,"cached":0.02, "out":1.25,    "priority":2.0},
    {"prefix":"gpt-5.4",         "in":2.50,"cached":0.25, "out":15.00,
        "in_long":5.00,"cached_long":0.50,"out_long":22.50,  "priority":2.0},
    {"prefix":"gpt-5.3-codex",   "in":1.75,"cached":0.175,"out":14.00,   "priority":2.0},
    {"prefix":"gpt-5.2-codex",   "in":1.75,"cached":0.175,"out":14.00,   "priority":2.0},
    {"prefix":"gpt-5.2-pro",     "in":21.00,"cached":0.0, "out":168.00,  "priority":2.0},
    {"prefix":"gpt-5.2",         "in":1.75,"cached":0.175,"out":14.00,   "priority":2.0},
    {"prefix":"gpt-5.1-codex-max","in":1.25,"cached":0.125,"out":10.00,  "priority":2.0},
    {"prefix":"gpt-5.1-codex",   "in":1.25,"cached":0.125,"out":10.00,   "priority":2.0},
    {"prefix":"gpt-5.1",         "in":1.25,"cached":0.125,"out":10.00,   "priority":2.0},
    {"prefix":"gpt-5-codex",     "in":1.25,"cached":0.125,"out":10.00,   "priority":2.0},
    {"prefix":"gpt-5-pro",       "in":15.00,"cached":0.0, "out":120.00,  "priority":2.0},
    {"prefix":"gpt-5-mini",      "in":0.25,"cached":0.025,"out":2.00,    "priority":1.8},
    {"prefix":"gpt-5-nano",      "in":0.05,"cached":0.005,"out":0.40,    "priority":1.0},
    {"prefix":"gpt-5",           "in":1.25,"cached":0.125,"out":10.00,   "priority":2.0},
]
LONG_CTX_THRESHOLD = 272_000  # tokens

def lookup_oai_price(model: str):
    if not model: return None
    m = model.lower()
    for p in PRICE_LIST_OAI:
        if m.startswith(p["prefix"]):
            return p
    return None

def codex_cost_components(model: str, in_t: int, cached_t: int, out_t: int,
                          reason_t: int, tier: str) -> dict:
    """Compute per-component costs for one Codex API call.
    Returns: {cost_in, cost_cached, cost_out_text, cost_out_reason}
    in_t includes cached_t (Codex format: input_tokens = total prompt size).
    """
    p = lookup_oai_price(model)
    if not p:
        return {"cost_in":0.0, "cost_cached":0.0, "cost_out_text":0.0, "cost_out_reason":0.0}
    long_ctx = (in_t > LONG_CTX_THRESHOLD) and ("in_long" in p)
    if long_ctx:
        in_rate, cached_rate, out_rate = p["in_long"], p["cached_long"], p["out_long"]
    else:
        in_rate, cached_rate, out_rate = p["in"], p["cached"], p["out"]
    # priority multiplier
    if tier == "priority":
        mult = p.get("priority", 1.0)
        in_rate *= mult; cached_rate *= mult; out_rate *= mult
    new_input_t = max(0, in_t - cached_t)
    text_out_t  = max(0, out_t - reason_t)
    return {
        "cost_in":         new_input_t * in_rate     / 1_000_000,
        "cost_cached":     cached_t    * cached_rate / 1_000_000,
        "cost_out_text":   text_out_t  * out_rate    / 1_000_000,
        "cost_out_reason": reason_t    * out_rate    / 1_000_000,
    }

# ─── Codex tier map (from logs_2.sqlite) ─────────────────────────────────────
# Each Codex API call sends a service_tier ("priority" / "default" / "auto").
# We treat priority → "priority", others → "standard".
# Tier can flip mid-session (user toggles fast mode between turns), so we build
# a sorted list per thread_id and binary-search by ts when assigning each
# token_count event its own tier.
_TIER_RE = re.compile(r'"service_tier"\s*:\s*"([^"]+)"')
_codex_tier_map = None  # {thread_id: [(ts_seconds, tier), ...]}

def invalidate_codex_tier_map():
    global _codex_tier_map
    _codex_tier_map = None

def get_codex_tier_map():
    global _codex_tier_map
    if _codex_tier_map is not None:
        return _codex_tier_map
    by_thread = defaultdict(list)
    if os.path.exists(CODEX_LOGS_DB):
        try:
            conn = sqlite3.connect(f"file:{CODEX_LOGS_DB}?mode=ro&immutable=1", uri=True)
            cur = conn.cursor()
            cur.execute("""
                SELECT thread_id, ts, feedback_log_body
                FROM logs
                WHERE thread_id IS NOT NULL
                  AND feedback_log_body LIKE '%service_tier%'
            """)
            for tid, ts, body in cur:
                m = _TIER_RE.search(body)
                if m:
                    raw = m.group(1)
                    tier = "priority" if raw == "priority" else "standard"
                    by_thread[tid].append((ts, tier))
            conn.close()
        except Exception:
            pass
    for tid in by_thread:
        by_thread[tid].sort()
    _codex_tier_map = dict(by_thread)
    return _codex_tier_map

def lookup_tier_for(thread_id: str, ts_ms: int) -> str:
    """Tier active for this thread at this ts. Default 'standard' if unknown."""
    m = get_codex_tier_map()
    arr = m.get(thread_id)
    if not arr:
        return "standard"
    ts_s = ts_ms // 1000
    # bisect for last entry with ts <= ts_s
    lo, hi = 0, len(arr)
    while lo < hi:
        mid = (lo + hi) >> 1
        if arr[mid][0] <= ts_s: lo = mid + 1
        else: hi = mid
    if lo == 0: return arr[0][1]  # before first known marker; assume same
    return arr[lo - 1][1]

# Cached deduped+sorted DataFrame (with cost column). Invalidated on refresh.
_global_df = None

def invalidate_df():
    global _global_df
    _global_df = None


def get_df() -> pl.DataFrame:
    """Materialize the global DataFrame on first call after a refresh.
    Merges Claude Code (~/.claude/projects) and Codex (~/.codex/sessions) rows.
    Per-row cost components are pre-computed at parse time."""
    global _global_df
    if _global_df is not None:
        return _global_df
    rows = []
    seen = set()
    with _lock:
        # Claude Code: dedup by msg_id (forks copy parent transcripts).
        for entry in _files.values():
            for r in entry["records"]:
                mid = r[0]
                if mid:
                    if mid in seen: continue
                    seen.add(mid)
                rows.append(r)
        # Codex: each rollout is a unique session, no cross-file duplication.
        for entry in _codex_files.values():
            for r in entry["records"]:
                rows.append(r)
    if not rows:
        _global_df = pl.DataFrame(schema=dict(RECORD_SCHEMA)).with_columns(pl.lit(0.0).alias("cost"))
        return _global_df
    df = pl.DataFrame(rows, schema=RECORD_SCHEMA, orient="row").sort("ts")
    df = df.with_columns(
        (pl.col("cost_in") + pl.col("cost_cached")
         + pl.col("cost_out_text") + pl.col("cost_out_reason")
         + pl.col("cost_cw5m") + pl.col("cost_cw1h") + pl.col("cost_cr")).alias("cost")
    )
    _global_df = df
    return df


def _format_bucket_key(bucket_utc_ms: int, off: int, g: str) -> str:
    local_secs = (bucket_utc_ms + off) // 1000
    tm = time.gmtime(local_secs)
    if g in ("1d", "day"):
        return time.strftime("%Y-%m-%d", tm)
    return time.strftime("%Y-%m-%d %H:%M", tm)


def _empty_bucket(key):
    return {"key": key, "calls":0, "in":0, "out":0, "cw5m":0, "cw1h":0, "cr":0,
            "cost":0.0, "mainCost":0.0, "subCost":0.0}


def aggregate(start_ms, end_ms, granularity, tz, fill_gaps=True, source="cc"):
    df = get_df()
    g = (granularity or "1h").lower()
    bucket_secs = SECONDS_IN_BUCKET.get(g, 3600)
    bucket_ms = bucket_secs * 1000
    off = tz_offset_ms(tz)
    floor_offset = -off  # add to ts to align floor with local-TZ bucket boundaries

    # Source filter: 'cc', 'codex', or 'all'
    if df.height and source in ("cc", "codex"):
        df = df.filter(pl.col("source") == source)

    # Slice window via predicate (polars uses SIMD on ints — instant)
    sub = df.filter((pl.col("ts") >= start_ms) & (pl.col("ts") < end_ms)) if df.height else df

    # Totals
    if sub.height > 0:
        main_part = sub.filter(pl.col("is_sub") == 0)
        sub_part  = sub.filter(pl.col("is_sub") == 1)
        totals = {
            "calls":  sub.height,
            "in":     int(sub["in_t"].sum() or 0),
            "cached": int(sub["cached_t"].sum() or 0),
            "out":    int(sub["out_t"].sum() or 0),
            "reason": int(sub["reason_t"].sum() or 0),
            "cw5m":   int(sub["cw5m"].sum() or 0),
            "cw1h":   int(sub["cw1h"].sum() or 0),
            "cr":     int(sub["cr"].sum() or 0),
            "cost":   float(sub["cost"].sum() or 0.0),
            "main": {"calls": main_part.height, "cost": float(main_part["cost"].sum() or 0.0)},
            "sub":  {"calls": sub_part.height,  "cost": float(sub_part["cost"].sum()  or 0.0)},
        }
    else:
        totals = {"calls":0,"in":0,"cached":0,"out":0,"reason":0,"cw5m":0,"cw1h":0,"cr":0,"cost":0.0,
                  "main":{"calls":0,"cost":0.0}, "sub":{"calls":0,"cost":0.0}}

    # By project
    by_proj_list = []
    if sub.height > 0:
        gp = (sub.group_by("project")
                 .agg([pl.len().alias("calls"),
                       pl.col("cost").sum().alias("cost"),
                       pl.col("out_t").sum().alias("out"),
                       pl.col("cr").sum().alias("cr")])
                 .sort("cost", descending=True))
        by_proj_list = [{"name": r["project"], "calls": r["calls"],
                         "cost": float(r["cost"] or 0.0),
                         "out": int(r["out"] or 0), "cr": int(r["cr"] or 0)}
                        for r in gp.iter_rows(named=True)]

    # By model
    by_model_list = []
    if sub.height > 0:
        gm = (sub.group_by("model")
                 .agg([pl.len().alias("calls"), pl.col("cost").sum().alias("cost")])
                 .sort("cost", descending=True))
        by_model_list = [{"name": r["model"], "calls": r["calls"],
                          "cost": float(r["cost"] or 0.0)}
                         for r in gm.iter_rows(named=True)]

    # Cost split — list of {label, key, val, color} so frontend just renders.
    # Slice composition differs per source: Claude Code has 5 cache tiers,
    # Codex has 4 (input/cached/output_text/output_reason).
    cost_split = []
    if sub.height > 0:
        if source == "codex":
            cost_split = [
                {"key":"cached",        "label":"Cached input",     "val": float(sub["cost_cached"].sum() or 0.0),     "color":"#0A0A0A"},
                {"key":"output",        "label":"Output (text)",    "val": float(sub["cost_out_text"].sum() or 0.0),   "color":"#FFD81F"},
                {"key":"reasoning",     "label":"Output (reasoning)","val":float(sub["cost_out_reason"].sum() or 0.0), "color":"#FF2BA0"},
                {"key":"input",         "label":"Input",            "val": float(sub["cost_in"].sum() or 0.0),         "color":"#FF6A1A"},
            ]
        else:
            cost_split = [
                {"key":"cr",    "label":"Cache read",     "val": float(sub["cost_cr"].sum()   or 0.0), "color":"#0A0A0A"},
                {"key":"cw1h",  "label":"Cache write 1h", "val": float(sub["cost_cw1h"].sum() or 0.0), "color":"#FF6A1A"},
                {"key":"out",   "label":"Output",         "val": float(sub["cost_out_text"].sum() or 0.0), "color":"#FFD81F"},
                {"key":"cw5m",  "label":"Cache write 5m", "val": float(sub["cost_cw5m"].sum() or 0.0), "color":"#FF2BA0"},
                {"key":"in",    "label":"Input",          "val": float(sub["cost_in"].sum()   or 0.0), "color":"#7C3AED"},
            ]
        cost_split = [c for c in cost_split if c["val"] > 0]

    # Buckets — int floor by bucket size, vectorized groupby
    bucket_rows = {}
    if sub.height > 0:
        sub2 = sub.with_columns(
            (((pl.col("ts") - floor_offset) // bucket_ms) * bucket_ms + floor_offset).alias("bucket_utc")
        )
        gb = (sub2.group_by("bucket_utc")
                  .agg([pl.len().alias("calls"),
                        pl.col("in_t").sum().alias("in"),
                        pl.col("cached_t").sum().alias("cached"),
                        pl.col("out_t").sum().alias("out"),
                        pl.col("reason_t").sum().alias("reason"),
                        pl.col("cw5m").sum().alias("cw5m"),
                        pl.col("cw1h").sum().alias("cw1h"),
                        pl.col("cr").sum().alias("cr"),
                        pl.col("cost").sum().alias("cost"),
                        (pl.col("cost") * (pl.col("is_sub") == 0).cast(pl.Float64)).sum().alias("mainCost"),
                        (pl.col("cost") * (pl.col("is_sub") == 1).cast(pl.Float64)).sum().alias("subCost"),
                        # Per-component cost sums for the BY-TOKEN chart view
                        pl.col("cost_in").sum().alias("c_in"),
                        pl.col("cost_cached").sum().alias("c_cached"),
                        pl.col("cost_out_text").sum().alias("c_out"),
                        pl.col("cost_out_reason").sum().alias("c_reason"),
                        pl.col("cost_cw5m").sum().alias("c_cw5m"),
                        pl.col("cost_cw1h").sum().alias("c_cw1h"),
                        pl.col("cost_cr").sum().alias("c_cr"),
                        ]))
        for r in gb.iter_rows(named=True):
            bucket_rows[r["bucket_utc"]] = r

    # Compose final bucket arrays (columnar — ~4× smaller wire size than per-row dicts).
    if fill_gaps:
        first = ((start_ms - floor_offset) // bucket_ms) * bucket_ms + floor_offset
        bucket_keys = list(range(first, end_ms, bucket_ms))
    else:
        bucket_keys = sorted(bucket_rows.keys())

    n = len(bucket_keys)
    ts_arr     = bucket_keys
    calls_arr  = [0] * n
    in_arr     = [0] * n
    cached_arr = [0] * n
    out_arr    = [0] * n
    reason_arr = [0] * n
    cw5m_arr   = [0] * n
    cw1h_arr   = [0] * n
    cr_arr     = [0] * n
    cost_arr   = [0.0] * n
    main_arr   = [0.0] * n
    sub_arr    = [0.0] * n
    cIn_arr     = [0.0] * n
    cCached_arr = [0.0] * n
    cOut_arr    = [0.0] * n
    cReason_arr = [0.0] * n
    cCw5m_arr   = [0.0] * n
    cCw1h_arr   = [0.0] * n
    cCr_arr     = [0.0] * n
    for i, bk in enumerate(bucket_keys):
        r = bucket_rows.get(bk)
        if r is None:
            continue
        calls_arr[i]  = r["calls"]
        in_arr[i]     = int(r["in"]     or 0)
        cached_arr[i] = int(r["cached"] or 0)
        out_arr[i]    = int(r["out"]    or 0)
        reason_arr[i] = int(r["reason"] or 0)
        cw5m_arr[i]   = int(r["cw5m"]   or 0)
        cw1h_arr[i]   = int(r["cw1h"]   or 0)
        cr_arr[i]     = int(r["cr"]     or 0)
        cost_arr[i]   = float(r["cost"]     or 0.0)
        main_arr[i]   = float(r["mainCost"] or 0.0)
        sub_arr[i]    = float(r["subCost"]  or 0.0)
        cIn_arr[i]     = float(r["c_in"]     or 0.0)
        cCached_arr[i] = float(r["c_cached"] or 0.0)
        cOut_arr[i]    = float(r["c_out"]    or 0.0)
        cReason_arr[i] = float(r["c_reason"] or 0.0)
        cCw5m_arr[i]   = float(r["c_cw5m"]   or 0.0)
        cCw1h_arr[i]   = float(r["c_cw1h"]   or 0.0)
        cCr_arr[i]     = float(r["c_cr"]     or 0.0)

    return {
        "totals": totals,
        "byProject": by_proj_list,
        "byModel":   by_model_list,
        "buckets": {
            "step":     bucket_ms,
            "ts":       ts_arr,        # bucket start, UTC ms
            "calls":    calls_arr,
            "in":       in_arr,
            "cached":   cached_arr,    # codex: cached_input_tokens
            "out":      out_arr,
            "reason":   reason_arr,    # codex: reasoning_output_tokens
            "cw5m":     cw5m_arr,      # cc only
            "cw1h":     cw1h_arr,      # cc only
            "cr":       cr_arr,        # cc only (cache_read)
            "cost":     cost_arr,
            "mainCost": main_arr,
            "subCost":  sub_arr,
            # Per-component cost arrays (for the BY-TOKEN chart view)
            "cIn":     cIn_arr,
            "cCached": cCached_arr,
            "cOut":    cOut_arr,
            "cReason": cReason_arr,
            "cCw5m":   cCw5m_arr,
            "cCw1h":   cCw1h_arr,
            "cCr":     cCr_arr,
        },
        "source":      source,
        "granularity": g,
        "costSplit":   cost_split,
        "firstRecordMs": int(df["ts"][0]) if df.height > 0 else None,
        "totalRecords": df.height,
        "files":       len(_files) + len(_codex_files),
        "lastRefresh": _last_full_refresh,
    }

# ---------- HTTP server ----------
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # silence default logging

    def _json(self, obj, status=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            try:
                body = get_html().encode()
            except FileNotFoundError:
                self.send_error(500, f"dashboard.html not found at {DASHBOARD_HTML}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/refresh":
            stats = refresh()
            return self._json(stats)
        if u.path == "/api/aggregate":
            q = parse_qs(u.query)
            try:
                start = int(q["from"][0]); end = int(q["to"][0])
            except Exception:
                return self._json({"error": "from/to (ms) required"}, 400)
            tz = q.get("tz", ["ET"])[0]
            granularity = q.get("granularity", ["hour"])[0]
            gaps = q.get("gaps", ["fill"])[0]    # "fill" | "skip"
            fill_gaps = gaps != "skip"
            source = q.get("source", ["cc"])[0]  # "cc" | "codex" | "all"
            if source not in ("cc", "codex", "all"):
                source = "cc"
            # cheap auto-refresh on stale cache
            if time.time() - _last_full_refresh > 30:
                refresh()
            return self._json(aggregate(start, end, granularity, tz, fill_gaps, source))
        self.send_error(404)

class ReusableTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.environ.get("TOKMETER_PORT", os.environ.get("CLAUDE_USAGE_PORT", DEFAULT_PORT))))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true", help="Don't auto-open browser")
    args = ap.parse_args()

    print(f"Scanning {ROOT} ...")
    t0 = time.time()
    stats = refresh(force=True)
    print(f"  {stats['scanned']} files scanned in {time.time()-t0:.1f}s")

    url = f"http://{args.host}:{args.port}/"
    print(f"\n  tokmeter")
    print(f"  → {url}\n")

    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    with ReusableTCPServer((args.host, args.port), Handler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nbye.")

if __name__ == "__main__":
    main()
