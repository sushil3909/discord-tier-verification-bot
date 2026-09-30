"""
Google Sheets integration for the Discord verification bot.

SQLite is always the source of truth. This module mirrors data into Google
Sheets so the team can see it. Every function fails quietly: if Google is
slow, misconfigured or down, verification still works and the record is still
safe in SQLite.

SETUP
-----
    pip install gspread google-auth

1. console.cloud.google.com -> new project
2. Enable "Google Sheets API" and "Google Drive API"
3. Credentials -> Create Credentials -> Service Account -> Keys -> JSON
4. Save the file as service_account.json in this folder
5. Open the JSON, copy the "client_email" value
6. Share your Google Sheet with that email as EDITOR
7. Put the Sheet ID (from its URL) in SHEET_ID below
"""

import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    # Point at the .env beside THIS file. load_dotenv() with no argument
    # searches the working directory, so a bot started by systemd or Task
    # Scheduler from elsewhere silently gets no settings at all.
    from pathlib import Path as _P
    load_dotenv(_P(__file__).resolve().parent / ".env")
except ImportError:
    pass

# Everything is resolved against THIS file's folder, not the working directory.
# A relative "service_account.json" is invisible to a bot started by systemd or
# Task Scheduler from somewhere else, and enabled() then returns False with no
# explanation - which is exactly how the Bot Submissions tab went quiet.
_HERE = Path(__file__).resolve().parent


def _path(value, default_name):
    if not value:
        return str(_HERE / default_name)
    p = Path(value)
    return str(p if p.is_absolute() else _HERE / p)


def sheet_id_from_url(url):
    """
    Pull the spreadsheet ID out of a pasted URL. Accepts a bare ID too.

    Both forms are accepted everywhere a sheet is named, because the natural
    thing to do is copy the address bar, and having one setting take a URL
    while another takes only the ID is a trap.
    """
    url = (url or "").strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", url):
        return url
    return ""


# The master sheet. Paste either the full URL or just the ID, here or as
# GSHEET_ID in .env.
SHEET_ID = sheet_id_from_url(os.getenv("GSHEET_ID", ""))
CREDS_FILE = _path(os.getenv("GOOGLE_CREDS_FILE"), "service_account.json")
DB_PATH = _path(os.getenv("JOURNEY_DB"), "client_journey.db")

SUBMISSIONS_TAB = "Bot Submissions"       # created automatically if absent
JOURNEY_TAB = "client_journey_export"     # must match the tab name in your sheet
BUILDS_TAB = "Build Info"                 # created automatically

# ----------------------------------------------------------------------
# Inner Circle - a different spreadsheet, owned by someone else.
#
# Paste the whole URL into .env as INNER_CIRCLE_URL. The tab is the one the
# team adds confirmed members to; everything else in that workbook (the 4+
# payouts working list, rejected, pending) is not a membership list and must
# not be read as one.
# ----------------------------------------------------------------------
INNER_CIRCLE_URL = os.getenv("INNER_CIRCLE_URL", "")
INNER_CIRCLE_TAB = os.getenv("INNER_CIRCLE_TAB", "Members")

# Last good copy. If the sheet is unreachable we fall back to this rather than
# reporting an empty membership list, which downstream would read as "nobody
# is in the Inner Circle any more".
INNER_CIRCLE_CACHE = _path(None, "_inner_circle_cache.csv")

# The bot records timestamps in UTC. This adds a second, local-time column so
# the sheet lines up with what you see in Discord. India is UTC+5:30.
LOCAL_TZ_OFFSET_HOURS = 5.5
LOCAL_TZ_LABEL = "IST"

SUBMISSION_HEADERS = [
    "Timestamp (UTC)", "Discord User ID", "Discord Username", "Email",
    "Account Number", "Twitter", "Instagram", "Customer Number",
    "Matched Tier", "Roles Assigned", "Status", "Notes",
    f"Local Time ({LOCAL_TZ_LABEL})",
]

_lock = threading.Lock()
_client = None
_warned = False
_tab_cache = {}          # opening the sheet costs read quota - do it once
_headers_checked = set()
_next_row = {}           # tab name -> (next free row, when we worked it out)

# Every submission is identified by (timestamp, discord user id). One member
# cannot produce two submissions in the same second, so this is unique in
# practice, and - crucially - it can be derived identically from a live dict
# and from a SQLite row. That is what lets the live append and the periodic
# reconcile write into the same tab without ever doubling a row.
_seen = {}               # tab name -> set of keys already in the sheet

# How long to trust the cached row position before re-reading it. Something
# else may have changed the sheet meanwhile - a resync, or an edit by hand -
# and writing to a stale position overwrites existing rows.
CURSOR_TTL_SECONDS = 300


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _to_local(ts):
    """'2026-07-28 15:33:14' (UTC) -> the same moment in local time."""
    if not ts:
        return ""
    from datetime import timedelta
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(str(ts).strip()[:19], fmt)
            return (dt + timedelta(hours=LOCAL_TZ_OFFSET_HOURS)).strftime(
                "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return ""


def _col_letter(n):
    """1 -> 'A', 26 -> 'Z', 27 -> 'AA'. chr(ord('A') + n - 1) breaks past 26."""
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


_disabled_reason_shown = False


def enabled():
    """
    True when we have both a sheet ID and a credentials file.

    When it is False the caller silently does nothing, so the reason is printed
    once. Without that, a missing SHEET_ID looks exactly like a working mirror
    that happens to have nothing to say.
    """
    global _disabled_reason_shown
    why = []
    if not SHEET_ID:
        why.append("SHEET_ID is not set (put GSHEET_ID in .env, or paste the "
                   "ID into sheets_sync.py)")
    if not os.path.exists(CREDS_FILE):
        why.append(f"credentials file not found at {CREDS_FILE}")
    if not why:
        return True
    if not _disabled_reason_shown:
        _disabled_reason_shown = True
        print("sheets: DISABLED - nothing will be written to the Google Sheet",
              flush=True)
        for w in why:
            print(f"   · {w}", flush=True)
    return False


def _get_client():
    """Lazily authorise. Returns None if unavailable, never raises."""
    global _client, _warned
    if _client is not None:
        return _client
    if not enabled():
        if not _warned:
            print("sheets: disabled (set SHEET_ID and add service_account.json "
                  "to enable)", flush=True)
            _warned = True
        return None
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        scopes = ["https://www.googleapis.com/auth/spreadsheets",
                  "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(CREDS_FILE, scopes=scopes)
        _client = gspread.authorize(creds)
        return _client
    except Exception as e:                                  # noqa: BLE001
        if not _warned:
            print(f"sheets: could not authorise ({e})", flush=True)
            _warned = True
        return None


def _ensure_headers(ws, headers):
    """Bring an existing tab's header row up to date without touching data."""
    try:
        current = ws.row_values(1)
    except Exception:                                       # noqa: BLE001
        return
    if current == headers:
        return
    if len(current) > len(headers):
        print(f"sheets: {ws.title!r} has {len(current)} header cells but the "
              f"code writes {len(headers)}. The tab is misaligned - run:\n"
              f"   python sheets_sync.py resync", flush=True)
    try:
        if ws.col_count < len(headers):
            ws.add_cols(len(headers) - ws.col_count)
        last = _col_letter(len(headers))
        ws.update(values=[headers], range_name=f"A1:{last}1",
                  value_input_option="RAW")
        print(f"sheets: updated the header row on {ws.title!r} "
              f"({len(headers)} columns)", flush=True)
    except Exception as e:                                  # noqa: BLE001
        print(f"sheets: could not update headers ({e})", flush=True)


def _get_tab(name, headers=None):
    """
    Cached. Google allows 60 read requests a minute, and opening the
    spreadsheet plus reading the header row costs three of them - so doing it
    per submission blows the quota the moment there is any volume.
    """
    if name in _tab_cache:
        return _tab_cache[name]
    gc = _get_client()
    if not gc:
        return None
    try:
        sh = gc.open_by_key(SHEET_ID)
        try:
            ws = sh.worksheet(name)
            if headers and name not in _headers_checked:
                _ensure_headers(ws, headers)
                _headers_checked.add(name)
        except Exception:                                   # noqa: BLE001
            ws = sh.add_worksheet(title=name, rows=1000, cols=max(len(headers or []), 20))
            if headers:
                # named args: gspread 6.x reversed the positional order
                ws.update(values=[headers], range_name="A1",
                          value_input_option="RAW")
                _headers_checked.add(name)
        _tab_cache[name] = ws
        return ws
    except Exception as e:                                  # noqa: BLE001
        print(f"sheets: cannot open tab {name!r} ({e})", flush=True)
        return None


def submission_key(row):
    """
    The identity of one submission: (timestamp, discord user id).

    Derived the same way whether `row` came from the bot in memory or from a
    SELECT on the submissions table, so both paths agree on what is already in
    the sheet. Leading apostrophes are stripped because the sheet stores the
    timestamp and the ID as text with one in front.
    """
    ts = str(row.get("ts") or "").strip().lstrip("'")
    uid = str(row.get("discord_user_id") or "").strip().lstrip("'")
    return ts, uid


def _load_seen(ws, force=False):
    """
    Read the key columns (A and B) once so we know what is already there.

    One read for the whole tab, not one per submission - the read quota is 60
    a minute and a launch-day rush would blow straight through that.
    """
    key = ws.title
    if key in _seen and not force:
        return _seen[key]
    keys = set()
    try:
        for r in ws.get_values("A2:B") or []:
            if len(r) >= 2:
                ts = str(r[0]).strip().lstrip("'")
                uid = str(r[1]).strip().lstrip("'")
                if ts or uid:
                    keys.add((ts, uid))
    except Exception as e:                                  # noqa: BLE001
        print(f"sheets: could not read existing keys ({e})", flush=True)
        if key in _seen:
            return _seen[key]
    _seen[key] = keys
    return keys


def _row_for(row):
    """One submission as a list of cell values."""
    ts = row.get("ts") or _now()
    return [
        # the apostrophe tells Sheets "this is text". Without it the timestamp
        # is parsed as a date and reads back in a different format, and the
        # 19-digit ID is rendered as 1.53E+18.
        "'" + str(ts),
        "'" + str(row.get("discord_user_id", "")),
        row.get("discord_username", ""),
        row.get("email", ""),
        "'" + str(row.get("account_number") or ""),
        row.get("twitter") or "",
        row.get("instagram") or "",
        row.get("customer_number") or "",
        row.get("matched_tier") or "",
        row.get("roles_assigned") or "",
        row.get("status", ""),
        row.get("notes") or "",
        "'" + _to_local(ts),
    ]


# ----------------------------------------------------------------------
# submissions - called on every verification attempt
# ----------------------------------------------------------------------
def _row_cursor(ws, force=False):
    """
    The next free row, tracked locally and re-checked periodically.

    We do not use append_row: the Sheets append API decides where to write by
    looking for a "table", and it has been observed writing over row 1 - the
    header - which then means every later submission overwrites the one
    before it.

    Caching the position outright is not safe either: a resync, or someone
    editing the sheet, moves the end of the data without the bot knowing, and
    the next write then lands on top of existing rows. So the cache expires.
    """
    import time
    key = ws.title
    cached = _next_row.get(key)
    fresh = (cached is not None
             and not force
             and time.time() - cached[1] < CURSOR_TTL_SECONDS)
    if fresh:
        return cached[0]

    try:
        used = len(ws.get_all_values())
    except Exception:                                       # noqa: BLE001
        used = cached[0] - 1 if cached else 1
    row = max(used + 1, 2)                                  # never row 1
    if cached and row != cached[0]:
        print(f"sheets: row position moved {cached[0]} -> {row} "
              f"(the sheet changed underneath us)", flush=True)
    _next_row[key] = (row, time.time())
    return row


def append_submission(row: dict):
    """
    Append one submission to the Bot Submissions tab.
    Safe to call from a background thread. Never raises.

    Expected keys: ts, discord_user_id, discord_username, email,
    account_number, twitter, instagram, customer_number, matched_tier,
    roles_assigned, status, notes

    `ts` matters. It must be the SAME timestamp that went into the submissions
    table, or this row and its SQLite original are two different records as far
    as the dedupe is concerned, and the reconcile below will add the row a
    second time.

    A failure here is not fatal: sync_submissions() picks up anything that did
    not make it, so a rate limit or a dropped connection costs a few minutes of
    delay rather than a missing row.
    """
    if not enabled():
        return False
    import time
    try:
        with _lock:
            ws = _get_tab(SUBMISSIONS_TAB, SUBMISSION_HEADERS)
            if not ws:
                return False

            key = submission_key(row)
            seen = _load_seen(ws)
            if key in seen:
                return True                    # already in the sheet

            last_col = _col_letter(len(SUBMISSION_HEADERS))
            values = _row_for(row)

            for attempt in range(3):
                r = _row_cursor(ws, force=(attempt > 0))
                if r > ws.row_count - 2:
                    try:
                        ws.add_rows(500)
                    except Exception:                       # noqa: BLE001
                        pass
                try:
                    ws.update(values=[values],
                              range_name=f"A{r}:{last_col}{r}",
                              value_input_option="USER_ENTERED")
                    _next_row[ws.title] = (r + 1, time.time())
                    seen.add(key)
                    return True
                except Exception as e:                      # noqa: BLE001
                    if "429" in str(e) and attempt < 2:
                        time.sleep(5 * (attempt + 1))
                        continue
                    if attempt < 2:
                        # our idea of the next row may be stale - re-read it
                        continue
                    raise
        return False
    except Exception as e:                                  # noqa: BLE001
        print(f"sheets: append_submission failed ({e}) - the row is safe in "
              f"SQLite and will be picked up by the next reconcile", flush=True)
        return False


# ----------------------------------------------------------------------
# client journey - run this nightly, NOT every cycle
# ----------------------------------------------------------------------
def push_client_journey(csv_path="client_journey_export.csv", chunk=5000):
    """
    Replace the Client Journey tab with the latest export.

    Only ever touches that one tab, so Bot Submissions is never disturbed.
    With 150k+ rows this takes several minutes - run it nightly.
    """
    if not enabled():
        print("sheets: disabled, skipping client journey push", flush=True)
        return False
    try:
        import pandas as pd
        df = pd.read_csv(csv_path, dtype=str).fillna("")
        gc = _get_client()
        if not gc:
            return False
        sh = gc.open_by_key(SHEET_ID)
        try:
            ws = sh.worksheet(JOURNEY_TAB)
            ws.clear()
        except Exception:                                   # noqa: BLE001
            ws = sh.add_worksheet(title=JOURNEY_TAB,
                                  rows=len(df) + 10, cols=len(df.columns) + 2)

        ws.resize(rows=len(df) + 10, cols=len(df.columns) + 2)
        ws.update(values=[df.columns.tolist()], range_name="A1",
                  value_input_option="RAW")

        rows = df.values.tolist()
        for i in range(0, len(rows), chunk):
            batch = rows[i:i + chunk]
            ws.update(values=batch, range_name=f"A{i + 2}",
                      value_input_option="RAW")
            print(f"sheets: {min(i + chunk, len(rows)):,}/{len(rows):,} rows",
                  flush=True)
        print(f"sheets: client journey pushed ({len(rows):,} rows)", flush=True)
        push_build_info()
        return True
    except Exception as e:                                  # noqa: BLE001
        print(f"sheets: push_client_journey failed ({e})", flush=True)
        return False


# ----------------------------------------------------------------------
def _submissions_from_db(db_path=None):
    """Every submission in SQLite, oldest first, with the customer number."""
    import sqlite3
    con = sqlite3.connect(db_path or DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("""
            SELECT s.*, c.customer_number
            FROM submissions s LEFT JOIN clients c ON c.email = s.email
            ORDER BY s.id
        """).fetchall()]
    finally:
        con.close()


def sync_submissions(db_path=None, chunk=500, quiet=False):
    """
    Bring the Bot Submissions tab up to date from SQLite.

    This is the safety net. Every role assignment is written to SQLite first
    and mirrored to the sheet second, so if the mirror fails - rate limit,
    network blip, a bot restart mid-write - the row exists in the database and
    nowhere else. Running this on a timer closes that gap without anyone
    noticing there was one.

    Idempotent: rows already in the sheet are matched on (timestamp, discord
    user id) and skipped, so it can be run as often as you like and after a
    partial failure.
    """
    if not enabled():
        return 0

    rows = _submissions_from_db(db_path)
    ws = _get_tab(SUBMISSIONS_TAB, SUBMISSION_HEADERS)
    if not ws:
        return 0

    with _lock:
        seen = _load_seen(ws, force=True)      # authoritative before we write
        todo, queued = [], set()
        for r in rows:
            k = submission_key(r)
            if k in seen or k in queued:
                continue                       # already there, or already queued
            queued.add(k)
            todo.append(r)

        if not todo:
            if not quiet:
                print(f"sheets: {len(rows):,} submissions, all present - "
                      f"nothing to add", flush=True)
            return 0

        if not quiet:
            print(f"sheets: {len(rows):,} submissions, {len(todo):,} missing "
                  f"from the sheet - adding", flush=True)

        import time
        last_col = _col_letter(len(SUBMISSION_HEADERS))
        added = 0
        for i in range(0, len(todo), chunk):
            batch = todo[i:i + chunk]
            values = [_row_for(r) for r in batch]

            # Write to explicit ranges rather than append_rows. The Sheets
            # append API places data after the "table" it detects, and a basic
            # filter on the header row redefines where that starts - which is
            # how rows ended up landing on top of each other before.
            for attempt in range(4):
                start_row = _row_cursor(ws, force=(attempt > 0))
                need = start_row + len(values) + 5
                if ws.row_count < need:
                    try:
                        ws.add_rows(need - ws.row_count)
                    except Exception:                       # noqa: BLE001
                        pass
                rng = f"A{start_row}:{last_col}{start_row + len(values) - 1}"
                try:
                    ws.update(values=values, range_name=rng,
                              value_input_option="USER_ENTERED")
                    _next_row[ws.title] = (start_row + len(values), time.time())
                    for r in batch:
                        seen.add(submission_key(r))
                    added += len(batch)
                    break
                except Exception as e:                      # noqa: BLE001
                    if "429" in str(e) and attempt < 3:
                        wait = 20 * (attempt + 1)
                        print(f"sheets: rate limited, waiting {wait}s",
                              flush=True)
                        time.sleep(wait)
                        continue
                    print(f"sheets: batch failed ({e})", flush=True)
                    break

    if not quiet:
        print(f"sheets: added {added:,} submission(s)", flush=True)
    return added


# kept so any existing scripts or notes that call backfill still work
def backfill_submissions(db_path=None, chunk=500):
    return sync_submissions(db_path=db_path, chunk=chunk)


def dedupe_submissions():
    """
    Remove duplicate rows already sitting in the tab, keeping the first of
    each. Works on the sheet alone, so rows added by hand survive.

    Use `resync` instead if you want the tab rebuilt from SQLite outright.
    """
    if not enabled():
        return 0
    ws = _get_tab(SUBMISSIONS_TAB, SUBMISSION_HEADERS)
    if not ws:
        return 0

    try:
        all_values = ws.get_all_values()
    except Exception as e:                                  # noqa: BLE001
        print(f"sheets: could not read the tab ({e})", flush=True)
        return 0
    if len(all_values) < 3:
        print("sheets: nothing to deduplicate", flush=True)
        return 0

    body = all_values[1:]
    kept, seen_keys, dropped = [], set(), 0
    for row in body:
        if not any(str(c).strip() for c in row):
            continue
        ts = str(row[0]).strip().lstrip("'") if len(row) > 0 else ""
        uid = str(row[1]).strip().lstrip("'") if len(row) > 1 else ""
        k = (ts, uid)
        if k in seen_keys:
            dropped += 1
            continue
        seen_keys.add(k)
        kept.append(row)

    if not dropped:
        print(f"sheets: {len(kept):,} rows, no duplicates found", flush=True)
        _seen[ws.title] = seen_keys
        return 0

    n = len(SUBMISSION_HEADERS)
    last_col = _col_letter(n)
    kept = [(r + [""] * n)[:n] for r in kept]
    try:
        ws.batch_clear([f"A2:{last_col}{max(ws.row_count, len(body) + 2)}"])
        if kept:
            ws.update(values=kept,
                      range_name=f"A2:{last_col}{len(kept) + 1}",
                      value_input_option="USER_ENTERED")
    except Exception as e:                                  # noqa: BLE001
        print(f"sheets: could not rewrite the tab ({e})", flush=True)
        return 0

    import time
    _seen[ws.title] = seen_keys
    _next_row[ws.title] = (len(kept) + 2, time.time())
    print(f"sheets: removed {dropped:,} duplicate row(s), {len(kept):,} remain",
          flush=True)
    return dropped


def selftest():
    """Check the whole chain and say exactly which link is broken."""
    print("sheets: checking the configuration")
    print(f"   folder          : {_HERE}")
    print(f"   SHEET_ID        : {SHEET_ID or '(not set)'}")
    print(f"   credentials     : {CREDS_FILE}"
          f"{'' if os.path.exists(CREDS_FILE) else '   <- MISSING'}")
    print(f"   database        : {DB_PATH}"
          f"{'' if os.path.exists(DB_PATH) else '   <- MISSING'}")
    if not enabled():
        return 1

    try:
        import json
        email = json.loads(open(CREDS_FILE).read()).get("client_email", "")
        print(f"   service account : {email}")
    except Exception:                                       # noqa: BLE001
        email = ""

    gc = _get_client()
    if not gc:
        print("   -> could not authorise. Check the JSON key is valid and that "
              "the Sheets and Drive APIs are enabled on the project.")
        return 1
    try:
        sh = gc.open_by_key(SHEET_ID)
        print(f"   spreadsheet     : {sh.title!r}")
        print(f"   tabs            : {[w.title for w in sh.worksheets()]}")
    except Exception as e:                                  # noqa: BLE001
        print(f"   -> cannot open the spreadsheet ({e})")
        if email:
            print(f"   -> share the sheet with {email} as an EDITOR")
        return 1

    ws = _get_tab(SUBMISSIONS_TAB, SUBMISSION_HEADERS)
    if not ws:
        return 1
    in_sheet = len(_load_seen(ws, force=True))
    try:
        in_db = len(_submissions_from_db())
    except Exception as e:                                  # noqa: BLE001
        print(f"   -> could not read the submissions table ({e})")
        return 1
    print(f"   submissions     : {in_db:,} in SQLite, {in_sheet:,} in the sheet")
    if in_db > in_sheet:
        print(f"   -> {in_db - in_sheet:,} missing. Run: "
              f"python sheets_sync.py sync")
    else:
        print("   -> the tab is up to date")
    return 0


BUILD_HEADERS = ["Built At (UTC)", "Reports From", "Reports To",
                 "Clients", "Upgraded", "Rejects", "Tier Breakdown"]


# ----------------------------------------------------------------------
# Inner Circle - read straight from the team's Google Sheet
# ----------------------------------------------------------------------
def _gid_from_url(url):
    m = re.search(r"[#&?]gid=(\d+)", url or "")
    return m.group(1) if m else ""


def _pc_via_service_account(sheet_id, tab):
    """
    Read the tab with the service account.

    Works on a sheet you only have view access to, as long as the owner has
    shared it with the service account email - Viewer is enough, they are not
    granting anyone edit rights.
    """
    gc = _get_client()
    if not gc:
        return None
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(tab)
    except Exception:                                       # noqa: BLE001
        names = [w.title for w in sh.worksheets()]
        raise RuntimeError(f"no tab named {tab!r} - the workbook has {names}")
    return ws.get_all_records()


def _pc_via_public_link(sheet_id, tab, gid=""):
    """
    Read the tab with no credentials at all.

    Only works if the sheet is shared as "anyone with the link can view". If
    it is restricted, Google serves a sign-in page instead of CSV, which we
    detect rather than parsing as data.
    """
    import io
    import urllib.parse
    import urllib.request

    urls = [f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq"
            f"?tqx=out:csv&sheet={urllib.parse.quote(tab)}"]
    if gid:
        urls.append(f"https://docs.google.com/spreadsheets/d/{sheet_id}"
                    f"/export?format=csv&gid={gid}")

    last = None
    for url in urls:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (verification-bot)"})
            raw = urllib.request.urlopen(req, timeout=30).read()
        except Exception as e:                              # noqa: BLE001
            last = e
            continue
        text = raw.decode("utf-8", "replace").lstrip()
        if text[:1] == "<" or "<HTML" in text[:400].upper():
            last = RuntimeError(
                "Google returned a sign-in page, not the sheet. The sheet is "
                "not shared by link, so either turn on 'anyone with the link "
                "can view', or ask the owner to share it with the service "
                "account as a Viewer.")
            continue
        import pandas as pd
        df = pd.read_csv(io.StringIO(text), dtype=str)
        if df.empty:
            last = RuntimeError("the sheet came back empty")
            continue
        return df.fillna("").to_dict("records")
    if last:
        raise last
    return None


def fetch_inner_circle(url=None, tab=None, use_cache=True):
    """
    Return (rows, source) for the Inner Circle membership tab.

    Tries the service account first, then the public link. `source` says which
    worked, or "cache" when neither did and we fell back to the last good copy
    - the caller needs to know the difference, because a stale list must not be
    treated as an authoritative one.
    """
    url = url if url is not None else INNER_CIRCLE_URL
    tab = tab or INNER_CIRCLE_TAB
    sheet_id = sheet_id_from_url(url)
    if not sheet_id:
        return None, "not configured"

    errors = []
    for name, fn in (("service account",
                      lambda: _pc_via_service_account(sheet_id, tab)),
                     ("public link",
                      lambda: _pc_via_public_link(sheet_id, tab,
                                                  _gid_from_url(url)))):
        try:
            rows = fn()
        except Exception as e:                              # noqa: BLE001
            errors.append(f"{name}: {e}")
            continue
        if rows:
            try:
                import pandas as pd
                pd.DataFrame(rows).to_csv(INNER_CIRCLE_CACHE, index=False)
            except Exception:                               # noqa: BLE001
                pass
            return rows, name

    for e in errors:
        print(f"inner circle: {e}", flush=True)

    if use_cache and os.path.exists(INNER_CIRCLE_CACHE):
        try:
            import pandas as pd
            rows = pd.read_csv(INNER_CIRCLE_CACHE, dtype=str).fillna("") \
                     .to_dict("records")
            age = (datetime.now().timestamp()
                   - os.path.getmtime(INNER_CIRCLE_CACHE)) / 3600
            print(f"inner circle: using the cached copy ({len(rows)} rows, "
                  f"{age:.0f}h old)", flush=True)
            return rows, "cache"
        except Exception:                                   # noqa: BLE001
            pass
    return None, "unavailable"


def inner_circle_report():
    """Check the Inner Circle sheet is readable and say what is in it."""
    print("inner circle: checking access")
    print(f"   url  : {INNER_CIRCLE_URL or '(INNER_CIRCLE_URL not set in .env)'}")
    print(f"   tab  : {INNER_CIRCLE_TAB!r}")
    sheet_id = sheet_id_from_url(INNER_CIRCLE_URL)
    print(f"   id   : {sheet_id or '(could not read an ID from that URL)'}")
    if not sheet_id:
        return 1

    rows, source = fetch_inner_circle()
    if not rows:
        print(f"   -> could not read the sheet ({source})")
        print("   -> either ask the owner to share it with the service account "
              "as a Viewer,")
        print("      or have them set link sharing to 'anyone with the link "
              "can view'.")
        return 1

    print(f"   read via: {source}")
    print(f"   rows    : {len(rows)}")
    cols = list(rows[0].keys())
    print(f"   columns : {cols}")
    email_col = _pc_column(cols, "email")
    cnum_col = _pc_column(cols, "cust", "number") or _pc_column(cols, "customer")
    print(f"   email   -> {email_col!r}")
    print(f"   cust no -> {cnum_col!r}")
    if not email_col and not cnum_col:
        print("   -> no email or customer number column, so members cannot be "
              "matched")
        return 1
    ids = {str(r.get(cnum_col, "")).strip() for r in rows} if cnum_col else set()
    print(f"   distinct customer numbers: {len(ids - {''})}")
    return 0


def _pc_column(columns, *keywords):
    """Find a column whose name loosely contains all the keywords."""
    for c in columns:
        low = re.sub(r"[^a-z]", "", str(c).lower())
        if all(re.sub(r"[^a-z]", "", k) in low for k in keywords):
            return c
    return None


def push_build_info(db_path=None, limit=200):
    """
    Write a small tab recording each build: when it ran and which date range
    the reports covered.

    This belongs on its own tab rather than as a column on the client data.
    The client table is cumulative - most rows were last touched by an earlier
    build - so stamping every row with the latest range would misrepresent it.
    """
    import sqlite3
    if not enabled():
        print("sheets: disabled", flush=True)
        return 0
    con = sqlite3.connect(db_path or DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT ran_at, IFNULL(date_from,'') AS date_from, "
            "IFNULL(date_to,'') AS date_to, clients, upgraded, rejects, summary "
            "FROM build_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    except Exception as e:                                  # noqa: BLE001
        print(f"sheets: could not read build_runs ({e})", flush=True)
        return 0
    finally:
        con.close()

    ws = _get_tab(BUILDS_TAB, BUILD_HEADERS)
    if not ws:
        return 0

    n = len(BUILD_HEADERS)
    last_col = _col_letter(n)
    values = [["'" + (r["ran_at"] or ""), r["date_from"], r["date_to"],
               r["clients"], r["upgraded"], r["rejects"], r["summary"]]
              for r in rows]
    try:
        ws.clear()
        need = len(values) + 10
        if ws.row_count < need:
            ws.add_rows(need - ws.row_count)
        ws.update(values=[BUILD_HEADERS], range_name=f"A1:{last_col}1",
                  value_input_option="RAW")
        if values:
            ws.update(values=values, range_name=f"A2:{last_col}{len(values) + 1}",
                      value_input_option="USER_ENTERED")
    except Exception as e:                                  # noqa: BLE001
        print(f"sheets: could not write {BUILDS_TAB!r} ({e})", flush=True)
        return 0
    print(f"sheets: wrote {len(values)} build record(s) to {BUILDS_TAB!r}",
          flush=True)
    return len(values)


def resync_submissions(db_path=None):
    """
    Clear the Bot Submissions tab and rewrite it from the database.

    Use this if the tab has picked up duplicates. SQLite is the record of
    truth, so rewriting from it gives exactly one row per submission.
    """
    if not enabled():
        print("sheets: disabled", flush=True)
        return 0

    rows = _submissions_from_db(db_path)

    ws = _get_tab(SUBMISSIONS_TAB, SUBMISSION_HEADERS)
    if not ws:
        return 0

    n = len(SUBMISSION_HEADERS)
    last_col = _col_letter(n)                 # 13 headers -> "M"

    # batch_clear on an explicit range wipes every cell, including any that
    # sit beyond the header. ws.clear() alone has been seen to leave a stale
    # column behind, which then shifts every row by one.
    try:
        wide = chr(ord("A") + max(ws.col_count, n + 5) - 1) if ws.col_count < 26 else "Z"
        ws.batch_clear([f"A1:{wide}{max(ws.row_count, len(rows) + 10)}"])
    except Exception:                                       # noqa: BLE001
        pass
    try:
        ws.clear()
    except Exception as e:                                  # noqa: BLE001
        print(f"sheets: could not clear the tab ({e})", flush=True)
        return 0

    try:
        if ws.col_count < n:
            ws.add_cols(n - ws.col_count)
        need = len(rows) + 10
        if ws.row_count < need:
            ws.add_rows(need - ws.row_count)
        ws.update(values=[SUBMISSION_HEADERS], range_name=f"A1:{last_col}1",
                  value_input_option="RAW")
    except Exception as e:                                  # noqa: BLE001
        print(f"sheets: could not write the header ({e})", flush=True)
        return 0

    try:
        wrote = ws.row_values(1)
        if wrote != SUBMISSION_HEADERS:
            print(f"sheets: WARNING - the header row reads {len(wrote)} columns, "
                  f"expected {n}", flush=True)
            print(f"   got     : {wrote}", flush=True)
            print(f"   expected: {SUBMISSION_HEADERS}", flush=True)
        else:
            print(f"sheets: header row written ({n} columns)", flush=True)
    except Exception:                                       # noqa: BLE001
        pass

    # Write to explicit ranges rather than appending.
    #
    # append_rows uses the Sheets "append" API, which places data after the
    # existing TABLE - and a basic filter on the header row defines that
    # table. If the filter range starts at column B, every appended row lands
    # at B no matter where the header is. Explicit ranges cannot drift.
    values = [_row_for(r) for r in rows]
    for i in range(0, len(values), 500):
        batch = values[i:i + 500]
        start = i + 2                          # row 1 is the header
        end = start + len(batch) - 1
        rng = f"A{start}:{last_col}{end}"
        for attempt in range(4):
            try:
                ws.update(values=batch, range_name=rng,
                          value_input_option="USER_ENTERED")
                break
            except Exception as e:                          # noqa: BLE001
                if "429" in str(e) and attempt < 3:
                    import time
                    wait = 20 * (attempt + 1)
                    print(f"sheets: rate limited, waiting {wait}s", flush=True)
                    time.sleep(wait)
                    continue
                print(f"sheets: batch failed ({e})", flush=True)
                break
    import time
    _next_row[ws.title] = (len(values) + 2, time.time())
    _seen[ws.title] = {submission_key(r) for r in rows}
    print(f"sheets: rewrote {len(values):,} submissions "
          f"(one row per record)", flush=True)
    return len(values)


USAGE = """usage: python sheets_sync.py <command>

  check      report what is configured and how far behind the sheet is
  pc         check the Inner Circle sheet is readable and show what is in it
  sync       add any submissions missing from Bot Submissions  (safe, repeatable)
  dedupe     remove duplicate rows already in Bot Submissions
  resync     wipe Bot Submissions and rebuild it from SQLite
  journey    replace the client journey tab from the export CSV  (slow, nightly)
  builds     refresh the Build Info tab
"""

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        raise SystemExit(selftest())
    if cmd in ("pc", "inner-circle", "innercircle"):
        raise SystemExit(inner_circle_report())
    if not enabled():
        raise SystemExit("Set SHEET_ID and add service_account.json first. "
                         "Run `python sheets_sync.py check` for details.")
    if cmd == "sync":
        sync_submissions()
    elif cmd == "dedupe":
        dedupe_submissions()
    elif cmd == "backfill":                      # old name for sync
        sync_submissions()
    elif cmd == "resync":
        resync_submissions()
    elif cmd == "journey":
        push_client_journey()
    elif cmd == "builds":
        push_build_info()
    else:
        print(USAGE)
