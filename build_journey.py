"""
Client Journey builder  -  Discord auto-role project
====================================================

Reads the four admin-portal exports and merges them into a SQLite database
that the Discord bot reads from, plus a CSV export laid out like the original
Client Journey sheet.

    python build_journey.py

KEY DESIGN RULE - MONOTONIC FLAGS
---------------------------------
ever_challenge / ever_funded / ever_payout only ever go 0 -> 1, never back.
Certificates are permanent achievements, so a member's tier can only rise.
This makes the job safe to run unattended: a failed, partial or truncated
export can add nothing, but it can never strip anyone's role.

The one exception is the blacklist, which is deliberate.

While still developing, delete client_journey.db before each run to get a
clean rebuild.  Once live, never delete it - that persistence is what
protects members' roles across a failed download.

TIER RULES (agreed)
-------------------
    inner_circle    listed in the curated Inner Circle sheet (highest tier)
    payout          any completed payout
    funded          holds or ever held an F account (incl. Direct, incl. breached)
    challenge       passed any C evaluation but never reached an F account
    account_holder  bought an account, never passed anything   (free trial excluded)
    verified        registered, nothing else

Blacklisted clients are flagged in the data but still receive the tier their
achievements earned.
"""

import glob
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

import pandas as pd

try:
    from dotenv import load_dotenv
    from pathlib import Path as _P
    load_dotenv(_P(__file__).resolve().parent / ".env")
except ImportError:
    pass

# ----------------------------------------------------------------------
# config
# ----------------------------------------------------------------------
ALL_CUSTOMERS    = "all_customers.csv"
ACCOUNT_PROGRESS = "account_progress.csv"
PASS_ANALYTICS   = "pass_analytics.csv"
PAYOUTS          = "payouts.csv"

# Inner Circle is a curated list maintained by the team, not derived from the
# data. It is read straight from the team's Google Sheet - put the URL in .env
# as INNER_CIRCLE_URL, and the tab as INNER_CIRCLE_TAB (default "Members").
#
# The setting below is the fallback used when no URL is configured or the
# sheet cannot be reached: a local .csv/.xlsx/.xls, or blank to auto-detect any
# file in this folder whose name contains "circle".
#
# If neither is available, existing Inner Circle flags are LEFT ALONE rather
# than cleared - a missing list must never strip anyone's role.
INNER_CIRCLE = "_Inner_Circle_Data.xlsx"

# Membership is add-only, so a bad read cannot remove anyone. This is only the
# point at which a short read is reported in the log and in rejects.csv, so a
# repeatedly truncated fetch does not go unnoticed.
INNER_CIRCLE_EXPECTED_RETAINED = 0.9

DB_PATH      = "client_journey.db"
REJECTS_PATH = "rejects.csv"
EXPORT_PATH  = "client_journey_export.csv"

STATUS_PASSED  = "hit profit target"
KNOWN_STATUSES = {"active", "breached", STATUS_PASSED}

# raw status -> what the sheet shows
DISPLAY_STATUS = {STATUS_PASSED: "Passed", "active": "Active", "breached": "Breached"}
# when a client has several accounts at one stage, show the best outcome
STATUS_PREF = {"Passed": 3, "Active": 2, "Breached": 1, "": 0}

TIER_RANK = {"none": 0, "verified": 1, "account_holder": 2,
             "challenge": 3, "funded": 4, "payout": 5, "inner_circle": 6}

# "None" is avoided as a label: pandas and Sheets both read it back as empty
CERT_OF_TIER = {
    "inner_circle": "Inner Circle",
    "payout": "Payout Certificate",
    "funded": "Funded Certificate",
    "challenge": "Challenge Certificate",
    "account_holder": "No Certificate",
    "verified": "No Certificate",
    "none": "Blacklisted",
}

# roles are stacked, highest tier downwards
# These strings must match the roles bot.py assigns. "Verified Member" is the
# bot's own role - deliberately distinct from ProBot's "Verified Role", which
# members get simply for reacting in the welcome channel.
ROLES_OF_TIER = {
    "inner_circle": "Inner Circle, Payout, Funded, Challenge, Account Holder, "
                    "Verified Member",
    "payout": "Payout, Funded, Challenge, Account Holder, Verified Member",
    "funded": "Funded, Challenge, Account Holder, Verified Member",
    "challenge": "Challenge, Account Holder, Verified Member",
    "account_holder": "Account Holder, Verified Member",
    "verified": "Verified Member",
    "none": "No Roles (Blacklisted)",
}

BLANK = "-"
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
rejects = []


def reject(source, reason, **fields):
    rejects.append({"source": source, "reason": reason, "when": NOW, **fields})


def log(msg):
    print(msg, flush=True)


# ----------------------------------------------------------------------
# normalisation
# ----------------------------------------------------------------------
def norm_cnum(v):
    """C0012026 -> C12026.  Both formats appear in the exports."""
    if v is None:
        return ""
    s = str(v).strip().upper()
    if s in ("", "NAN", "NONE"):
        return ""
    if s.startswith("C"):
        return "C" + s.lstrip("C").lstrip("0")
    return s


def norm_email(v):
    if v is None:
        return ""
    s = str(v).strip().lower()
    return "" if s in ("", "nan", "none") else s


def norm_login(v):
    """Account numbers arrive as '2200342.0' from float coercion."""
    if v is None:
        return ""
    s = str(v).strip()
    if s in ("", "nan", "None", "NaN"):
        return ""
    return re.sub(r"\.0$", "", s)


def norm_text(v):
    """Plan names contain literal tabs and doubled spaces. Flatten them."""
    if v is None:
        return ""
    s = str(v).replace("\t", " ").replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return "" if s.lower() in ("", "nan", "none") else s


def truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "y", "t")


# ----------------------------------------------------------------------
# plan classification
# ----------------------------------------------------------------------
def classify_plan(plan_name):
    """
    Returns one of:
        FT      free trial            - grants nothing
        DIRECT  instant funding       - counts as a funded account
        C1 C2 C3 C  challenge stages  - a pass grants the challenge flag
        F       funded account        - grants the funded flag
        TEST    internal test plan    - ignored
        OTHER   unrecognised          - ignored, but logged to rejects
    """
    p = norm_text(plan_name).lower()
    if not p:
        return None
    if re.search(r"\btest(ing)?\b", p):
        return "TEST"
    if re.search(r"\bft\b", p) or "free trial" in p:
        return "FT"

    # Stage markers first: they are the most specific, and some plans say
    # "Phase 1" instead of "C-1" (e.g. "Futures Phase 1 - 25K").
    if re.search(r"\bc\s*-?\s*1\b", p) or re.search(r"\bphase\s*-?\s*1\b", p):
        return "C1"
    if re.search(r"\bc\s*-?\s*2\b", p) or re.search(r"\bphase\s*-?\s*2\b", p):
        return "C2"
    if re.search(r"\bc\s*-?\s*3\b", p) or re.search(r"\bphase\s*-?\s*3\b", p):
        return "C3"

    if re.search(r"\bdirect\b", p):
        return "DIRECT"

    # "F 5000", "F- 25K" and "F100000" - the last has no space before the
    # digits, so \bf\b alone does not match it.
    if re.search(r"\bf\b", p) or re.search(r"\bf\s*-?\s*\d", p):
        return "F"
    if re.search(r"\bc\b", p) or re.search(r"\bc\s*-?\s*\d", p):
        return "C"
    return "OTHER"


CHALLENGE_KINDS = {"C1", "C2", "C3", "C"}
FUNDED_KINDS    = {"F", "DIRECT"}

# which sheet column each plan kind is displayed in
STAGE_OF_KIND = {"C1": "phase1", "C": "phase1", "FT": "phase1",
                 "C2": "phase2", "C3": "phase2",
                 "F": "funded", "DIRECT": "funded"}


def set_stage(c, stage, acct, plan, status, is_ft):
    """
    A client can hold many accounts at one stage but the sheet has one column
    per stage, so keep the most representative: a real plan always beats a
    free trial, then the best status wins (Passed > Active > Breached).
    """
    new = {"acct": acct or BLANK, "plan": plan or BLANK, "status": status, "ft": is_ft}
    cur = c["stages"][stage]
    if cur is None or (cur["ft"] and not is_ft):
        c["stages"][stage] = new
        return
    if is_ft and not cur["ft"]:
        return
    if STATUS_PREF.get(status, 0) > STATUS_PREF.get(cur["status"], 0):
        c["stages"][stage] = new


# ----------------------------------------------------------------------
# load
# ----------------------------------------------------------------------
def read_csv(path, needed=None):
    if not os.path.exists(path):
        sys.exit(f"ERROR: {path} not found. Run this from the folder holding the exports.")
    df = pd.read_csv(path, low_memory=False, dtype=str)
    if needed:
        missing = [c for c in needed if c not in df.columns]
        if missing:
            sys.exit(f"ERROR: {path} is missing expected columns: {missing}")
    log(f"  {path}: {len(df):,} rows")
    return df


def load_customers():
    """
    All Customers is the seed - every registered client, whether or not they
    ever bought anything.  Only whitelisted columns are read; the export also
    carries PASSWORD, phone, dateOfBirth and tokens which must never leave
    this machine.
    """
    keep = ["customerNumber", "customerId", "firstName", "lastName", "email",
            "country", "isAffiliate", "isBlackLister", "insertedCST",
            "loyaltyLevelId", "loyaltyPoints"]
    df = read_csv(ALL_CUSTOMERS, needed=["customerNumber", "email"])
    df = df[[c for c in keep if c in df.columns]].copy()

    clients, by_cnum = {}, {}
    for r in df.to_dict("records"):
        email = norm_email(r.get("email"))
        cnum  = norm_cnum(r.get("customerNumber"))
        if not email:
            reject("all_customers", "blank email", customer_number=cnum)
            continue

        name = " ".join(x for x in [norm_text(r.get("firstName")),
                                    norm_text(r.get("lastName"))] if x)
        clients[email] = {
            "email": email,
            "customer_number": cnum,
            "customer_id": norm_text(r.get("customerId")),
            "client_name": name,
            "country": norm_text(r.get("country")),
            "is_affiliate": int(truthy(r.get("isAffiliate"))),
            "is_blacklisted": int(truthy(r.get("isBlackLister"))),
            "registered_at": norm_text(r.get("insertedCST")),
            "loyalty_level": norm_text(r.get("loyaltyLevelId")),
            "ever_challenge": 0,
            "ever_funded": 0,
            "ever_payout": 0,
            "has_account": 0,
            "is_inner_circle": 0,
            "risk_flags": set(),
            "accounts": set(),
            "stages": {"phase1": None, "phase2": None, "funded": None},
        }
        if cnum:
            by_cnum[cnum] = email
    return clients, by_cnum


def load_existing(clients, by_cnum, acct_to_email):
    """
    Pull the clients already in the database into the lookup indexes.

    Without this, a short date range breaks: a client who registered in May
    but passed a challenge in July appears in July's Pass Analytics but not in
    July's All Customers, so their pass would be rejected and lost.

    Only identity and flags are loaded. Rows are NOT written back unless this
    run actually touches them, so untouched clients keep their existing
    display columns.
    """
    if not os.path.exists(DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT email, customer_number, customer_id, client_name, country, "
            "is_affiliate, is_blacklisted, registered_at, loyalty_level, "
            "ever_challenge, ever_funded, ever_payout, has_account, "
            "COALESCE(is_inner_circle,0) AS is_inner_circle, risk_flags, "
            "account_numbers, phase1_account, phase1_plan, phase1_status, "
            "phase2_account, phase2_plan, phase2_status, "
            "funded_account, funded_plan, funded_status, first_seen "
            "FROM clients").fetchall()
        existing = {r["email"]: dict(r) for r in rows}
        for login, email in conn.execute("SELECT login, email FROM accounts"):
            acct_to_email.setdefault(login, email)
        conn.close()
    except Exception as e:                                  # noqa: BLE001
        log(f"  could not read existing clients ({e})")
        return {}

    for email, r in existing.items():
        if r["customer_number"]:
            by_cnum.setdefault(r["customer_number"], email)
    log(f"  {len(existing):,} clients already in the database")
    return existing


def hydrate(clients, existing, email):
    """Bring one existing client into this run so it can be updated."""
    if email in clients:
        return clients[email]
    r = existing.get(email)
    if r is None:
        return None
    c = {
        "email": email,
        "customer_number": r["customer_number"] or "",
        "customer_id": r["customer_id"] or "",
        "client_name": r["client_name"] or "",
        "country": r["country"] or "",
        "is_affiliate": r["is_affiliate"] or 0,
        "is_blacklisted": r["is_blacklisted"] or 0,
        "registered_at": r["registered_at"] or "",
        "loyalty_level": r["loyalty_level"] or "",
        "ever_challenge": r["ever_challenge"] or 0,
        "ever_funded": r["ever_funded"] or 0,
        "ever_payout": r["ever_payout"] or 0,
        "has_account": r["has_account"] or 0,
        "is_inner_circle": r["is_inner_circle"] or 0,
        "risk_flags": set(f.strip() for f in (r["risk_flags"] or "").split(",")
                          if f.strip()),
        "accounts": set(a.strip() for a in (r["account_numbers"] or "").split(",")
                        if a.strip()),
        "stages": {"phase1": None, "phase2": None, "funded": None},
    }
    for stage in ("phase1", "phase2", "funded"):
        acct = r[f"{stage}_account"]
        plan = r[f"{stage}_plan"]
        status = r[f"{stage}_status"]
        if plan and plan != BLANK:
            c["stages"][stage] = {"acct": acct or BLANK, "plan": plan,
                                  "status": status or "", "ft": False}
    clients[email] = c
    return c


# ----------------------------------------------------------------------
# account progress
# ----------------------------------------------------------------------
def apply_account_progress(clients, by_cnum, acct_to_email, existing):
    """
    Each row holds one account in slot 1, plus - for accounts that progressed -
    the same journey restated in slots 2..4.  Those repeats are duplicates of
    other rows' slot 1, so everything is deduplicated by login before use.
    A handful of accounts appear only in slots 2-4, so all
    four are scanned rather than slot 1 alone.
    """
    df = read_csv(ACCOUNT_PROGRESS, needed=["customerNumber", "planType1"])
    accounts = {}          # login -> dict, slot 1 wins
    unknown_plans, unknown_status = {}, {}

    for r in df.to_dict("records"):
        cnum = norm_cnum(r.get("customerNumber"))
        for slot in (1, 2, 3, 4):
            plan  = norm_text(r.get(f"planType{slot}"))
            login = norm_login(r.get(f"loginType{slot}"))
            if not plan and not login:
                continue
            status = norm_text(r.get(f"statusType{slot}")).lower()
            if status and status not in KNOWN_STATUSES:
                unknown_status[status] = unknown_status.get(status, 0) + 1

            kind = classify_plan(plan)
            if kind == "OTHER":
                unknown_plans[plan] = unknown_plans.get(plan, 0) + 1

            key = login or f"__nologin__{cnum}_{plan}_{slot}"
            if key not in accounts or slot == 1:
                accounts[key] = {"login": login, "cnum": cnum, "plan": plan,
                                 "kind": kind, "status": status}

    log(f"  deduplicated to {len(accounts):,} distinct accounts")

    unmatched = 0
    for a in accounts.values():
        email = by_cnum.get(a["cnum"])
        if email and email not in clients:
            hydrate(clients, existing, email)
        if not email or email not in clients:
            unmatched += 1
            reject("account_progress", "customerNumber not in all_customers",
                   customer_number=a["cnum"], account=a["login"], plan=a["plan"])
            continue

        c = clients[email]
        kind = a["kind"]
        if a["login"]:
            c["accounts"].add(a["login"])
            acct_to_email[a["login"]] = email

        if kind in ("TEST", "OTHER", None):
            continue

        # display columns - free trials are shown but grant nothing
        stage = STAGE_OF_KIND.get(kind)
        if stage:
            set_stage(c, stage, a["login"], a["plan"],
                      DISPLAY_STATUS.get(a["status"], ""), kind == "FT")

        if kind == "FT":
            continue

        c["has_account"] = 1
        if kind in FUNDED_KINDS:
            c["ever_funded"] = 1
        elif kind in CHALLENGE_KINDS and a["status"] == STATUS_PASSED:
            c["ever_challenge"] = 1

    if unmatched:
        log(f"  WARNING: {unmatched:,} accounts had no matching customer")
    for plan, n in sorted(unknown_plans.items(), key=lambda x: -x[1]):
        reject("account_progress", "unrecognised plan name", plan=plan, count=n)
    for st, n in unknown_status.items():
        reject("account_progress", "UNKNOWN STATUS - check tier rules", status=st, count=n)
        log(f"  WARNING: unknown status {st!r} on {n:,} accounts")


# ----------------------------------------------------------------------
# pass analytics  (every row is a pass)
# ----------------------------------------------------------------------
def apply_pass_analytics(clients, acct_to_email, existing):
    df = read_csv(PASS_ANALYTICS, needed=["email", "accountNumber"])
    matched = 0
    for r in df.to_dict("records"):
        email = norm_email(r.get("email"))
        if email not in clients and not hydrate(clients, existing, email):
            email = acct_to_email.get(norm_login(r.get("accountNumber")), "")
            if email:
                hydrate(clients, existing, email)
        if not email or email not in clients:
            reject("pass_analytics", "no matching client",
                   account=norm_login(r.get("accountNumber")),
                   plan=norm_text(r.get("planName")))
            continue

        kind = classify_plan(r.get("planName"))
        if kind in ("FT", "TEST"):
            continue                              # free-trial passes grant nothing

        c = clients[email]
        matched += 1
        c["has_account"] = 1
        if kind in FUNDED_KINDS:
            c["ever_funded"] = 1
        else:
            c["ever_challenge"] = 1
        if truthy(r.get("everFunded")):
            c["ever_funded"] = 1
        flags = norm_text(r.get("riskflags"))
        if flags:
            c["risk_flags"].update(f.strip() for f in flags.split(",") if f.strip())
    log(f"  matched {matched:,} pass records")


# ----------------------------------------------------------------------
# payouts  (completed only)
# ----------------------------------------------------------------------
def apply_payouts(clients, acct_to_email, existing):
    df = read_csv(PAYOUTS, needed=["email", "CompletedDate"])
    completed = skipped = matched = 0
    for r in df.to_dict("records"):
        if not norm_text(r.get("CompletedDate")):
            skipped += 1                          # pending or rejected
            continue
        completed += 1

        email = norm_email(r.get("email"))
        if email not in clients and not hydrate(clients, existing, email):
            email = acct_to_email.get(norm_login(r.get("login")), "")
            if email:
                hydrate(clients, existing, email)
        if not email or email not in clients:
            reject("payouts", "no matching client", account=norm_login(r.get("login")),
                   payout_id=norm_text(r.get("PayoutId")))
            continue

        clients[email]["ever_payout"] = 1
        matched += 1

    log(f"  {completed:,} completed, {skipped:,} pending/incomplete (skipped), "
        f"{matched:,} matched")


# ----------------------------------------------------------------------
# inner circle  (curated list, matched on email then customer number)
# ----------------------------------------------------------------------
def find_inner_circle_file():
    if INNER_CIRCLE:
        if os.path.exists(INNER_CIRCLE):
            return INNER_CIRCLE
        # the configured name may have a different extension on disk
        stem = os.path.splitext(INNER_CIRCLE)[0]
        for ext in (".xlsx", ".xls", ".csv"):
            if os.path.exists(stem + ext):
                return stem + ext
    for pat in ("*.csv", "*.xlsx", "*.xls"):
        for f in sorted(glob.glob(pat)):
            name = os.path.basename(f).lower()
            if "circle" in name and "payout" not in name:
                return f
    return None


def _match_col(df, *keywords):
    """Find a column whose name loosely contains all the keywords."""
    for c in df.columns:
        low = re.sub(r"[^a-z]", "", str(c).lower())
        if all(re.sub(r"[^a-z]", "", k) in low for k in keywords):
            return c
    return None


def _inner_circle_current_count():
    """How many members are flagged right now, for the sanity check below."""
    if not os.path.exists(DB_PATH):
        return 0
    try:
        with sqlite3.connect(DB_PATH) as c:
            return c.execute("SELECT COUNT(*) FROM clients "
                             "WHERE COALESCE(is_inner_circle,0) = 1").fetchone()[0]
    except Exception:                                       # noqa: BLE001
        return 0


def load_inner_circle_rows():
    """
    Get the Inner Circle list, preferring the team's Google Sheet.

    Returns (dataframe, description) or (None, reason). The local file is kept
    as a fallback so a network problem does not stop the build.
    """
    try:
        import sheets_sync
    except ImportError:
        sheets_sync = None

    if sheets_sync is not None and sheets_sync.INNER_CIRCLE_URL:
        rows, source = sheets_sync.fetch_inner_circle()
        if rows:
            return (pd.DataFrame(rows).astype(str),
                    f"{sheets_sync.INNER_CIRCLE_TAB!r} tab via {source}")
        log(f"  could not read the Inner Circle sheet ({source})")

    path = find_inner_circle_file()
    if not path:
        return None, "no sheet URL and no local file"
    if path.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(path, dtype=str), path
    return pd.read_csv(path, dtype=str, low_memory=False), path


def apply_inner_circle(clients, by_cnum, existing):
    """Returns True if the list was read, False if it was absent."""
    df, source = load_inner_circle_rows()
    if df is None:
        log(f"  no inner circle list ({source}) - existing flags left unchanged")
        return False
    log(f"  {source}: {len(df):,} rows")

    # A short read used to be dangerous, because the list was mirrored exactly
    # and anyone missing from it lost the role. Membership is add-only now, so
    # the worst a truncated read can do is add fewer people this run and catch
    # the rest on the next one. Still worth saying out loud.
    previous = _inner_circle_current_count()
    if previous and len(df) < previous * INNER_CIRCLE_EXPECTED_RETAINED:
        log(f"  NOTE: {len(df):,} rows read against {previous:,} members "
            f"already flagged. The read may have been cut short - nobody is "
            f"removed either way.")
        reject("inner_circle", "fewer rows than members already flagged",
               rows=len(df), currently_flagged=previous)

    email_col = _match_col(df, "email")
    cnum_col = _match_col(df, "cust", "number") or _match_col(df, "customer")
    if not email_col and not cnum_col:
        log(f"  WARNING: no email or customer number column in {source} "
            f"(found {list(df.columns)}) - skipped")
        return False

    matched = 0
    for r in df.to_dict("records"):
        email = norm_email(r.get(email_col)) if email_col else ""
        if email not in clients and not hydrate(clients, existing, email):
            cnum = norm_cnum(r.get(cnum_col)) if cnum_col else ""
            email = by_cnum.get(cnum, "")
            if email:
                hydrate(clients, existing, email)
        if not email or email not in clients:
            reject("inner_circle", "no matching client",
                   email=norm_email(r.get(email_col)) if email_col else "",
                   customer_number=norm_cnum(r.get(cnum_col)) if cnum_col else "")
            continue
        clients[email]["is_inner_circle"] = 1
        matched += 1

    log(f"  matched {matched:,} inner circle members")
    return True


# ----------------------------------------------------------------------
# tier
# ----------------------------------------------------------------------
def resolve_tier(c):
    # is_blacklisted is recorded for reference but does not affect the tier -
    # every client gets the role their achievements earned.
    if c["is_inner_circle"]:
        return "inner_circle"
    if c["ever_payout"]:
        return "payout"
    if c["ever_funded"]:
        return "funded"
    if c["ever_challenge"]:
        return "challenge"
    if c["has_account"]:
        return "account_holder"
    return "verified"


def validate(clients):
    """A payout with no funded account should be impossible - flag any."""
    n = 0
    for c in clients.values():
        if c["ever_payout"] and not c["ever_funded"]:
            n += 1
            reject("validation", "payout but no funded account",
                   email=c["email"], customer_number=c["customer_number"])
    if n:
        log(f"  WARNING: {n} client(s) have a payout but no funded account")


# ----------------------------------------------------------------------
# sqlite  -  monotonic merge
# ----------------------------------------------------------------------
COLUMNS = [
    ("email", "TEXT PRIMARY KEY"), ("customer_number", "TEXT"),
    ("customer_id", "TEXT"), ("client_name", "TEXT"), ("country", "TEXT"),
    ("is_affiliate", "INTEGER DEFAULT 0"), ("is_blacklisted", "INTEGER DEFAULT 0"),
    ("registered_at", "TEXT"), ("loyalty_level", "TEXT"),
    ("ever_challenge", "INTEGER DEFAULT 0"), ("ever_funded", "INTEGER DEFAULT 0"),
    ("ever_payout", "INTEGER DEFAULT 0"), ("has_account", "INTEGER DEFAULT 0"),
    ("is_inner_circle", "INTEGER DEFAULT 0"),
    ("highest_tier", "TEXT"), ("risk_flags", "TEXT"), ("account_numbers", "TEXT"),
    ("phase1_account", "TEXT"), ("phase1_plan", "TEXT"), ("phase1_status", "TEXT"),
    ("phase2_account", "TEXT"), ("phase2_plan", "TEXT"), ("phase2_status", "TEXT"),
    ("funded_account", "TEXT"), ("funded_plan", "TEXT"), ("funded_status", "TEXT"),
    ("first_seen", "TEXT"), ("last_updated", "TEXT"),
]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS clients (
    {", ".join(f"{n} {t}" for n, t in COLUMNS)}
);
CREATE INDEX IF NOT EXISTS idx_clients_cnum ON clients(customer_number);
CREATE INDEX IF NOT EXISTS idx_clients_tier ON clients(highest_tier);

CREATE TABLE IF NOT EXISTS accounts (
    login  TEXT PRIMARY KEY,
    email  TEXT
);

CREATE TABLE IF NOT EXISTS build_runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at    TEXT,
    clients   INTEGER,
    upgraded  INTEGER,
    rejects   INTEGER,
    summary   TEXT,
    date_from TEXT,
    date_to   TEXT
);
"""

NAMES = [n for n, _ in COLUMNS]
# is_inner_circle is in here too. It used to mirror the sheet exactly so the
# team could remove someone, but that made a deleted row indistinguishable
# from a deliberate removal - and rows get deleted by accident. Membership is
# now add-only like every other achievement, and removals go through
# /innercircle remove in Discord, which is a decision someone has to make on
# purpose.
MONOTONIC = {"ever_challenge", "ever_funded", "ever_payout", "has_account",
             "is_inner_circle"}


def build_upsert(pc_loaded=True):
    """
    Every flag uses MAX() so none of them can ever fall back to 0.

    That includes Inner Circle. A short, truncated or completely failed read
    of the membership sheet can therefore add people and can never remove
    anyone, which is the same guarantee the portal exports have always had.
    """
    updates = []
    for n in NAMES:
        if n in ("email", "first_seen"):
            continue
        if n in MONOTONIC:
            updates.append(f"{n} = MAX(clients.{n}, excluded.{n})")
        elif n == "registered_at":
            updates.append("registered_at = COALESCE(clients.registered_at, "
                           "excluded.registered_at)")
        else:
            updates.append(f"{n} = excluded.{n}")
    return (f"INSERT INTO clients ({', '.join(NAMES)}) "
            f"VALUES ({', '.join('?' * len(NAMES))}) "
            f"ON CONFLICT(email) DO UPDATE SET {', '.join(updates)}")


def download_range():
    """What date range the reports covered, if the downloader recorded it."""
    try:
        import json
        info = json.loads(open("download_info.json").read())
        return info.get("date_from", ""), info.get("date_to", "")
    except Exception:                                       # noqa: BLE001
        return "", ""


def migrate(conn):
    """Add any column missing from an older database."""
    for col in ("date_from", "date_to"):
        try:
            conn.execute(f"ALTER TABLE build_runs ADD COLUMN {col} TEXT")
        except Exception:                                   # noqa: BLE001
            pass                                            # already there

    have = {r[1] for r in conn.execute("PRAGMA table_info(clients)")}
    if not have:
        return
    for name, decl in COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE clients ADD COLUMN {name} "
                         f"{decl.replace('PRIMARY KEY', '')}")
            log(f"  migrated: added column {name}")


def stage_cols(c, stage):
    s = c["stages"][stage]
    if not s:
        return BLANK, BLANK, BLANK
    return s["acct"], s["plan"], s["status"] or BLANK


def write_db(clients, acct_to_email, pc_loaded):
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    migrate(conn)

    before = dict(conn.execute("SELECT email, highest_tier FROM clients"))

    rows = []
    for c in clients.values():
        p1 = stage_cols(c, "phase1")
        p2 = stage_cols(c, "phase2")
        fu = stage_cols(c, "funded")
        rows.append((
            c["email"], c["customer_number"], c["customer_id"], c["client_name"],
            c["country"], c["is_affiliate"], c["is_blacklisted"], c["registered_at"],
            c["loyalty_level"], c["ever_challenge"], c["ever_funded"], c["ever_payout"],
            c["has_account"], c["is_inner_circle"], resolve_tier(c),
            ", ".join(sorted(c["risk_flags"])),
            ", ".join(sorted(c["accounts"])),
            p1[0], p1[1], p1[2], p2[0], p2[1], p2[2], fu[0], fu[1], fu[2],
            NOW, NOW,
        ))
    conn.executemany(build_upsert(pc_loaded), rows)

    # recompute tier from the merged flags, not this run's flags alone
    conn.execute("""
        UPDATE clients SET highest_tier = CASE
            WHEN is_inner_circle = 1 THEN 'inner_circle'
            WHEN ever_payout     = 1 THEN 'payout'
            WHEN ever_funded     = 1 THEN 'funded'
            WHEN ever_challenge  = 1 THEN 'challenge'
            WHEN has_account     = 1 THEN 'account_holder'
            ELSE 'verified' END
    """)

    conn.executemany(
        "INSERT INTO accounts (login, email) VALUES (?,?) "
        "ON CONFLICT(login) DO UPDATE SET email = excluded.email",
        list(acct_to_email.items()),
    )

    after = dict(conn.execute("SELECT email, highest_tier FROM clients"))
    upgraded = [e for e, t in after.items()
                if e in before and TIER_RANK[t] > TIER_RANK.get(before[e], 0)]
    dropped  = [e for e, t in after.items()
                if e in before and TIER_RANK[t] < TIER_RANK.get(before[e], 0)]

    counts = dict(conn.execute(
        "SELECT highest_tier, COUNT(*) FROM clients GROUP BY highest_tier"))
    d_from, d_to = download_range()
    conn.execute("INSERT INTO build_runs "
                 "(ran_at, clients, upgraded, rejects, summary, date_from, date_to) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (NOW, len(after), len(upgraded), len(rejects), str(counts),
                  d_from, d_to))
    conn.commit()
    export(conn)
    conn.close()
    return counts, upgraded, dropped


def export(conn):
    """CSV laid out like the original Client Journey sheet."""
    df = pd.read_sql_query("""
        SELECT customer_number, client_name, email,
               phase1_account, phase1_plan, phase1_status,
               phase2_account, phase2_plan, phase2_status,
               funded_account, funded_plan, funded_status,
               ever_payout, highest_tier, is_inner_circle, account_numbers,
               last_updated
        FROM clients
        ORDER BY CASE highest_tier
                    WHEN 'inner_circle' THEN 0 WHEN 'payout' THEN 1
                    WHEN 'funded' THEN 2 WHEN 'challenge' THEN 3
                    WHEN 'account_holder' THEN 4
                    WHEN 'verified' THEN 5 ELSE 6 END,
                 client_name
    """, conn)

    pc = df["is_inner_circle"].fillna(0).astype(int) == 1
    roles = df["highest_tier"].map(ROLES_OF_TIER)

    out = pd.DataFrame({
        "Customer Number":     df["customer_number"],
        "Client Name":         df["client_name"],
        "Email":               df["email"],
        "Phase 1  Account":    df["phase1_account"],
        "Phase 1 Plan":        df["phase1_plan"],
        "Phase 1 Status":      df["phase1_status"],
        "Phase 2 Account":     df["phase2_account"],
        "Phase 2 Plan":        df["phase2_plan"],
        "Phase 2 Status":      df["phase2_status"],
        "Funded Account":      df["funded_account"],
        "Funded Plan":         df["funded_plan"],
        "Funded Status":       df["funded_status"],
        "Has Payout":          df["ever_payout"].map({1: "Yes", 0: "No"}),
        "Inner Circle":        pc.map({True: "Yes", False: "No"}),
        "Highest Certificate": df["highest_tier"].map(CERT_OF_TIER),
        "Discord Roles":       roles,
        "All Account Numbers": df["account_numbers"],
        "Last Updated (UTC)":  df["last_updated"],
    })
    out = out.fillna(BLANK).replace("", BLANK)

    out.to_csv(EXPORT_PATH, index=False)


# ----------------------------------------------------------------------
def main():
    log(f"\nClient Journey build - {NOW} UTC\n" + "=" * 52)
    _f, _t = download_range()
    if _f:
        log(f"reports cover {_f} -> {_t}")

    log("\nloading all_customers (seed)")
    clients, by_cnum = load_customers()
    log(f"  {len(clients):,} clients seeded")

    acct_to_email = {}
    log("\nloading existing database")
    existing = load_existing(clients, by_cnum, acct_to_email)

    log("\nloading account_progress")
    apply_account_progress(clients, by_cnum, acct_to_email, existing)

    log("\nloading pass_analytics")
    apply_pass_analytics(clients, acct_to_email, existing)

    log("\nloading payouts")
    apply_payouts(clients, acct_to_email, existing)

    log("\nloading inner circle")
    pc_loaded = apply_inner_circle(clients, by_cnum, existing)

    log("\nvalidating")
    validate(clients)

    log(f"\nwriting database ({len(clients):,} client rows touched this run)")
    counts, upgraded, dropped = write_db(clients, acct_to_email, pc_loaded)

    if rejects:
        pd.DataFrame(rejects).to_csv(REJECTS_PATH, index=False)

    log("\n" + "=" * 52 + "\nTIERS")
    for tier in ("inner_circle", "payout", "funded", "challenge",
                 "account_holder", "verified", "none"):
        log(f"  {tier:<16}{counts.get(tier, 0):>9,}")
    log(f"  {'TOTAL':<16}{sum(counts.values()):>9,}")



    log(f"\nupgraded since last run: {len(upgraded):,}")
    if dropped:
        log(f"  *** {len(dropped)} client(s) DROPPED a tier - investigate, this "
            f"should only happen via blacklisting")
    log(f"rejects: {len(rejects):,}" + (f"  -> {REJECTS_PATH}" if rejects else ""))
    log(f"database: {DB_PATH}\nexport:   {EXPORT_PATH}\n")


if __name__ == "__main__":
    main()
