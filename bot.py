"""
Discord Auto Role Assignment Bot
================================

"Acme Trading" in the user-facing text below is a placeholder brand name.

Members press a Verify button, submit their email, and the bot looks them up in
client_journey.db (built by build_journey.py) and assigns the matching roles.

SETUP
-----
1.  pip install discord.py python-dotenv

2.  Create a file called `.env` next to this script:

        DISCORD_TOKEN=your_bot_token_here

    Never put the token in this file, and never commit .env anywhere.

3.  Fill in the CONFIG section below with your IDs.
    (Discord: User Settings > Advanced > Developer Mode, then right-click >
     Copy ID on the server, each role and each channel.)

4.  Make sure client_journey.db is in the same folder, or set DB_PATH.

5.  In the Discord Developer Portal, under Bot > Privileged Gateway Intents,
    enable SERVER MEMBERS INTENT. Role assignment will not work without it.

6.  Drag the bot's own role ABOVE all five assignable roles in
    Server Settings > Roles. Discord will not let a bot assign a role at or
    above its own position.

7.  python bot.py
    Then run /postverify once in #get-your-roles-here to place the button.
    You only ever need to do this once - the button survives restarts.
"""

import asyncio
import os
import re
import sqlite3
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Optional: mirrors submissions into Google Sheets. If the file is missing or
# not configured, everything still works - SQLite is the source of truth.
try:
    import sheets_sync
except ImportError:
    sheets_sync = None

# ======================================================================
# CONFIG  -  fill these in
# ======================================================================

GUILD_ID = 0            # your server ID

# NOTE: "verified_member" is the bot's own role, NOT the "Verified Role" that
# ProBot hands out for reacting in #start-here. Those are different things and
# must stay separate: ProBot's is on 20,000+ members who never used this bot,
# and it must never appear here, or the reconciler could strip it from them.
ROLE_IDS = {
    "payout":         0,
    "funded":         0,
    "challenge":      0,
    "account_holder": 0,
    "verified_member": 0,   # <- paste the new "Verified Member" role ID here
    "inner_circle":    0,   # granted from the curated Inner Circle sheet
}

# Roles the bot must never add or remove. Leave empty unless you have a role
# that overlaps with the ones above and is assigned by hand.
PROTECTED_ROLE_IDS = []

# ProBot's "Verified Role" - the one members get for reacting in the welcome
# channel. Around 20,000 people hold it. Only used by /migrate_verified, which
# strips it from members who claimed through this bot, and from nobody else.
OLD_VERIFIED_ROLE_ID = 0

CHANNEL_IDS = {
    "verify":       0,   # where the Claim button is posted
    "winners":      0,   # #certificate-winners
    "logs":         0,   # #bot-logs (private). Optional - leave 0 to log to
                         # the console only. Submissions are still recorded in
                         # the database and Google Sheet either way.
}

DB_PATH = "client_journey.db"

# --- behaviour -------------------------------------------------------
# Announce every tier. Blacklisted ("none") is never announced.
ANNOUNCE_TIERS = {"inner_circle", "payout", "funded", "challenge",
                  "account_holder", "verified"}

# Master switch. Set False for launch day so everyone can verify quietly,
# then flip it on (or use /announcements on) so only genuine new
# achievements get posted.
ANNOUNCEMENTS_ENABLED = True

# Seconds between announcement posts. Discord throttles a channel at roughly
# 5 messages per 5 seconds; 2.0 stays comfortably under that.
ANNOUNCE_INTERVAL = 2.0

# If the email is not found, also try the account number they typed.
# Rescues people who registered under a different address, but it is a second
# way to claim someone else's tier. Every use is logged with a warning.
ALLOW_ACCOUNT_FALLBACK = True

# If a member is already linked to one email and submits a different one,
# refuse and send them to support. Set True to let them re-link themselves.
ALLOW_SELF_RELINK = False

# How often the background job re-checks every verified member for upgrades.
SYNC_INTERVAL_HOURS = 4

# The bot also watches client_journey.db and syncs as soon as it changes, so a
# rebuild takes effect within a minute instead of waiting for the next cycle.
WATCH_DB_SECONDS = 60

# The same job also retries people whose email was not found - a new customer
# who claimed before their record reached the database. It looks back this far
# through the submission log.
RETRY_FAILED_DAYS = 7

SUPPORT_NOTE = "Please contact support and we'll sort it out for you."

# Google Chat webhook for problems that need a person to act. Put the URL in
# .env as GCHAT_WEBHOOK_URL - in the space, Apps & integrations > Webhooks.
# Only real problems go here; routine activity stays in the log channel.
GCHAT_WEBHOOK_URL = os.getenv("GCHAT_WEBHOOK_URL", "")

# ======================================================================

TIER_LABEL = {
    "payout":         "Payout",
    "funded":         "Funded",
    "challenge":      "Challenge",
    "account_holder": "Account Holder",
    "verified":        "Verified",
    "verified_member": "Verified Member",
    "none":           "Blacklisted",
    "inner_circle":   "Inner Circle",
}

# Roles are stacked: each tier grants itself and everything below it.
# Every tier ends in verified_member: claiming any role means we have
# confirmed this person is a client.
TIER_STACK = {
    "inner_circle":   ["inner_circle", "payout", "funded", "challenge",
                       "account_holder", "verified_member"],
    "payout":         ["payout", "funded", "challenge", "account_holder",
                       "verified_member"],
    "funded":         ["funded", "challenge", "account_holder", "verified_member"],
    "challenge":      ["challenge", "account_holder", "verified_member"],
    "account_holder": ["account_holder", "verified_member"],
    "verified":       ["verified_member"],
    "none":           [],
}

TIER_RANK = {"none": 0, "verified": 1, "account_holder": 2,
             "challenge": 3, "funded": 4, "payout": 5, "inner_circle": 6}

TIER_EMOJI = {
    "inner_circle":   "👑",
    "payout":         "🏆",
    "funded":         "📈",
    "challenge":      "🎯",
    "account_holder": "🎫",
    "verified":        "✅",
    "verified_member": "✅",
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Social links must be full profile URLs, not bare handles - a handle alone
# is ambiguous and cannot be opened.
SOCIAL_RE = {
    "Twitter / X": re.compile(
        r"^https?://(www\.)?(twitter|x)\.com/[A-Za-z0-9_.]+/?(\?.*)?$", re.I),
    "Instagram": re.compile(
        r"^https?://(www\.)?instagram\.com/[A-Za-z0-9_.]+/?(\?.*)?$", re.I),
}
SOCIAL_EXAMPLE = {
    "Twitter / X": "https://x.com/yourhandle",
    "Instagram": "https://instagram.com/yourhandle",
}


def check_social(value, kind):
    """
    Returns (cleaned, error). A blank value is fine - these fields are
    optional. Anything else has to be a real profile link.
    """
    v = (value or "").strip()
    if not v:
        return "", None
    if not re.match(r"^https?://", v, re.I):
        # bare handle, or a link without the scheme
        if re.match(r"^(www\.)?(twitter\.com|x\.com|instagram\.com)/", v, re.I):
            v = "https://" + v.lstrip("/")
        else:
            return None, (
                f"**{kind} profile** needs to be a full link, not a username.\n"
                f"For example: `{SOCIAL_EXAMPLE[kind]}`")
    if not SOCIAL_RE[kind].match(v):
        return None, (
            f"**{kind} profile** doesn't look like a valid profile link.\n"
            f"For example: `{SOCIAL_EXAMPLE[kind]}`")
    return v, None


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


_notified = {}


def notify(text, key=None, cooldown=3600):
    """
    Send a problem to Google Chat. Fails quietly - a broken webhook must never
    stop the bot.

    `key` throttles repeats: the same key will not resend within `cooldown`
    seconds, so a recurring fault does not flood the space.
    """
    if not GCHAT_WEBHOOK_URL:
        return
    import time as _t
    if key:
        last = _notified.get(key, 0)
        if _t.time() - last < cooldown:
            return
        _notified[key] = _t.time()
    try:
        import json as _json
        import urllib.request as _url
        req = _url.Request(
            GCHAT_WEBHOOK_URL,
            data=_json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=UTF-8"})
        _url.urlopen(req, timeout=10).read()
    except Exception as e:                                  # noqa: BLE001
        print(f"google chat notify failed: {e}", flush=True)


# ======================================================================
# database
# ======================================================================
BOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS discord_links (
    discord_user_id  TEXT PRIMARY KEY,
    email            TEXT,
    customer_number  TEXT,
    current_tier     TEXT,
    is_inner_circle  INTEGER DEFAULT 0,
    verified_at      TEXT,
    last_synced      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_links_email ON discord_links(email);

CREATE TABLE IF NOT EXISTS submissions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT,
    discord_user_id  TEXT,
    discord_username TEXT,
    email            TEXT,
    account_number   TEXT,
    twitter          TEXT,
    instagram        TEXT,
    matched_tier     TEXT,
    roles_assigned   TEXT,
    status           TEXT,
    notes            TEXT
);
"""


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.executescript(BOT_SCHEMA)
        # migrate older databases created before Inner Circle existed
        have = {r[1] for r in c.execute("PRAGMA table_info(discord_links)")}
        if "is_inner_circle" not in have:
            c.execute("ALTER TABLE discord_links "
                      "ADD COLUMN is_inner_circle INTEGER DEFAULT 0")


def _lookup(email, account=None):
    """Return the client row, plus how we matched. Runs in a worker thread."""
    with _conn() as c:
        row = c.execute(
            "SELECT email, customer_number, client_name, highest_tier, "
            "is_blacklisted, COALESCE(is_inner_circle, 0) AS is_inner_circle "
            "FROM clients WHERE email = ?", (email,)).fetchone()
        if row:
            return dict(row), "email"

        if account and ALLOW_ACCOUNT_FALLBACK:
            row = c.execute(
                "SELECT c.email, c.customer_number, c.client_name, "
                "c.highest_tier, c.is_blacklisted, "
                "COALESCE(c.is_inner_circle, 0) AS is_inner_circle "
                "FROM accounts a "
                "JOIN clients c ON c.email = a.email WHERE a.login = ?",
                (account,)).fetchone()
            if row:
                return dict(row), "account"
    return None, None


def _link_owner(email):
    with _conn() as c:
        r = c.execute("SELECT discord_user_id FROM discord_links WHERE email = ?",
                      (email,)).fetchone()
        return r["discord_user_id"] if r else None


def _get_link(uid):
    with _conn() as c:
        r = c.execute("SELECT * FROM discord_links WHERE discord_user_id = ?",
                      (str(uid),)).fetchone()
        return dict(r) if r else None


def _save_link(uid, email, cnum, tier, inner_circle=0):
    with _conn() as c:
        c.execute("""
            INSERT INTO discord_links
                (discord_user_id, email, customer_number, current_tier,
                 is_inner_circle, verified_at, last_synced)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(discord_user_id) DO UPDATE SET
                email = excluded.email,
                customer_number = excluded.customer_number,
                current_tier = excluded.current_tier,
                is_inner_circle = excluded.is_inner_circle,
                last_synced = excluded.last_synced
        """, (str(uid), email, cnum, tier, int(inner_circle), now(), now()))


def _unlink(uid):
    with _conn() as c:
        cur = c.execute("DELETE FROM discord_links WHERE discord_user_id = ?",
                        (str(uid),))
        return cur.rowcount


def _log_submission(**kw):
    cols = ("ts", "discord_user_id", "discord_username", "email", "account_number",
            "twitter", "instagram", "matched_tier", "roles_assigned", "status", "notes")
    kw.setdefault("ts", now())
    with _conn() as c:
        c.execute(f"INSERT INTO submissions ({','.join(cols)}) "
                  f"VALUES ({','.join('?' * len(cols))})",
                  tuple(kw.get(k) for k in cols))


def _pending_retries(days):
    """
    Members whose claim failed only because we had no record of their email,
    and who have not verified since. Once a rebuild brings their data in, the
    sync can finish the job without them touching the form again.
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        # Any failure that might now succeed - not just "email not found".
        # A permission error, a Discord outage or a rate limit all leave the
        # member with no link row, so /syncall cannot see them either. The
        # only failures worth excluding are the deliberate refusals, which
        # will fail again for the same reason.
        rows = c.execute("""
            SELECT s.discord_user_id, s.email, MAX(s.ts) AS last_try
            FROM submissions s
            WHERE s.status = 'Failed'
              AND s.ts >= ?
              AND s.email IS NOT NULL AND s.email <> ''
              AND IFNULL(s.notes, '') NOT LIKE '%already claimed%'
              AND IFNULL(s.notes, '') NOT LIKE '%already linked%'
              AND IFNULL(s.notes, '') NOT LIKE '%invalid email%'
              AND IFNULL(s.notes, '') NOT LIKE '%invalid twitter%'
              AND IFNULL(s.notes, '') NOT LIKE '%invalid instagram%'
              AND NOT EXISTS (
                    SELECT 1 FROM discord_links l
                    WHERE l.discord_user_id = s.discord_user_id)
            GROUP BY s.discord_user_id, s.email
            ORDER BY last_try
        """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]


def _all_links():
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM discord_links")]


def _tier_for_email(email):
    with _conn() as c:
        r = c.execute("SELECT highest_tier, is_blacklisted, customer_number, "
                      "COALESCE(is_inner_circle, 0) AS is_inner_circle "
                      "FROM clients WHERE email = ?", (email,)).fetchone()
        return dict(r) if r else None


# ======================================================================
# bot
# ======================================================================
intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!tier ", intents=intents)
announce_queue: asyncio.Queue = asyncio.Queue()
announcements_on = ANNOUNCEMENTS_ENABLED


def guild():
    return bot.get_guild(GUILD_ID)


async def log(msg):
    ch = bot.get_channel(CHANNEL_IDS.get("logs") or 0)
    if ch:
        try:
            await ch.send(msg[:1990])
        except discord.HTTPException:
            pass
    print(msg, flush=True)


async def apply_roles(member, tier):
    """Set the member's roles to exactly the stack for this tier."""
    g = member.guild
    protected = {rid for rid in PROTECTED_ROLE_IDS if rid}
    managed = {rid for rid in ROLE_IDS.values() if rid} - protected
    target = {ROLE_IDS[k] for k in TIER_STACK.get(tier, []) if ROLE_IDS.get(k)}
    target -= protected
    current = {r.id for r in member.roles}

    add = [g.get_role(r) for r in (target - current)]
    remove = [g.get_role(r) for r in ((managed & current) - target)]
    add = [r for r in add if r]
    remove = [r for r in remove if r]

    try:
        for attempt in range(3):
            try:
                if add:
                    await member.add_roles(*add, reason=f"Acme Trading tier: {tier}")
                if remove:
                    await member.remove_roles(*remove,
                                              reason=f"Acme Trading tier: {tier}")
                break
            except discord.DiscordServerError:
                # 5xx from Discord - transient, worth another go
                if attempt == 2:
                    raise
                await asyncio.sleep(2 * (attempt + 1))
    except discord.Forbidden as e:
        me = g.me
        bot_top = me.top_role
        above = [r.name for r in (add + remove) if r >= bot_top]
        detail = []
        if above:
            detail.append(f"these roles sit at or above my own role "
                          f"**{bot_top.name}**: {', '.join(above)}")
        if member.top_role >= bot_top:
            detail.append(f"their highest role **{member.top_role.name}** "
                          f"(position {member.top_role.position}) is at or above "
                          f"mine (position {bot_top.position})")
        if member.id == g.owner_id:
            detail.append("they are the server owner — Discord never lets a bot "
                          "change the owner's roles")
        if not me.guild_permissions.manage_roles:
            detail.append("I do not have the Manage Roles permission")

        # None of the usual causes matched, so report what Discord actually
        # said rather than guessing.
        if not detail:
            detail.append(
                f"Discord said: `{getattr(e, 'text', str(e))}` "
                f"(code {getattr(e, 'code', '?')})")
            detail.append(
                f"their roles: {', '.join(r.name for r in member.roles) or 'none'}")
            detail.append(
                f"was adding: {', '.join(r.name for r in add) or 'none'}; "
                f"removing: {', '.join(r.name for r in remove) or 'none'}")

        await log(f"❌ **Cannot assign roles** to {member.mention}: "
                  + "; ".join(detail))
        notify(f"⚠️ Acme Trading role bot could not assign roles to "
               f"{member.display_name}: " + "; ".join(detail),
               key="assign-forbidden")
        raise
    return [r.name for r in add], [r.name for r in remove]


def stack_names(tier):
    return ", ".join(TIER_LABEL[k] for k in TIER_STACK.get(tier, [])) or "none"


async def queue_announcement(member, new_tier, old_tier):
    if not announcements_on or new_tier not in ANNOUNCE_TIERS:
        return
    label = TIER_LABEL.get(new_tier, new_tier)
    emoji = TIER_EMOJI.get(new_tier, "🎉")
    moved_up = bool(old_tier and old_tier != new_tier
                    and TIER_RANK.get(old_tier, 0) > 0)
    if new_tier == "inner_circle":
        text = (f"👑 {member.mention} has joined the **Inner Circle**!"
                if moved_up else
                f"👑 {member.mention} just claimed their **Inner Circle** role!")
    elif moved_up:
        text = (f"{emoji} {member.mention} moved up from "
                f"**{TIER_LABEL[old_tier]}** to **{label}**!")
    else:
        text = f"{emoji} {member.mention} just claimed their **{label}** role!"
    await announce_queue.put(text)


async def announcement_worker():
    """Drains the queue slowly so verification never waits on Discord's
    channel rate limit (~5 messages / 5 seconds)."""
    await bot.wait_until_ready()
    ch = bot.get_channel(CHANNEL_IDS["winners"])
    while not bot.is_closed():
        text = await announce_queue.get()
        try:
            if ch:
                await ch.send(text)
        except discord.HTTPException as e:
            print(f"announcement failed: {e}", flush=True)
        finally:
            announce_queue.task_done()
        await asyncio.sleep(ANNOUNCE_INTERVAL)


# ----------------------------------------------------------------------
# the core path: look someone up and give them their roles
# ----------------------------------------------------------------------
async def verify_member(member, email, account="", twitter="", instagram="",
                        source="button"):
    """Returns (ok: bool, message: str). Handles every outcome."""
    uid = str(member.id)
    email = (email or "").strip().lower()
    account = re.sub(r"\.0$", "", (account or "").strip())

    def record(status, tier=None, roles=None, notes=None, cnum=None):
        _log_submission(discord_user_id=uid, discord_username=str(member),
                        email=email, account_number=account, twitter=twitter,
                        instagram=instagram, matched_tier=tier,
                        roles_assigned=roles, status=status, notes=notes)
        # Mirror to Google Sheets. SQLite has already been written, so a
        # failure here costs nothing but a missing row in the sheet.
        if sheets_sync is not None and sheets_sync.enabled():
            try:
                sheets_sync.append_submission({
                    "discord_user_id": uid, "discord_username": str(member),
                    "email": email, "account_number": account,
                    "twitter": twitter, "instagram": instagram,
                    "customer_number": cnum, "matched_tier": tier,
                    "roles_assigned": roles, "status": status, "notes": notes,
                })
            except Exception as e:                          # noqa: BLE001
                print(f"sheets mirror failed: {e}", flush=True)

    if not EMAIL_RE.match(email):
        await asyncio.to_thread(record, "Failed", notes="invalid email format")
        return False, "That doesn't look like a valid email address. Please try again."

    twitter, err = check_social(twitter, "Twitter / X")
    if err:
        await asyncio.to_thread(record, "Failed", notes="invalid twitter link")
        return False, err + "\n\nLeave it blank if you'd rather not share it."
    instagram, err = check_social(instagram, "Instagram")
    if err:
        await asyncio.to_thread(record, "Failed", notes="invalid instagram link")
        return False, err + "\n\nLeave it blank if you'd rather not share it."

    existing = await asyncio.to_thread(_get_link, uid)
    if existing and existing["email"] != email and not ALLOW_SELF_RELINK:
        await asyncio.to_thread(record, "Failed", notes="already linked to another email")
        await log(f"⚠️ {member.mention} is linked to `{existing['email']}` but "
                  f"submitted `{email}`")
        return False, ("Your Discord account is already linked to a different email "
                       f"address. {SUPPORT_NOTE}")

    owner = await asyncio.to_thread(_link_owner, email)
    if owner and owner != uid:
        await asyncio.to_thread(record, "Failed", notes=f"email already claimed by {owner}")
        await log(f"🚫 {member.mention} tried `{email}` — already linked to <@{owner}>")
        return False, ("That email address is already linked to another Discord "
                       f"account. {SUPPORT_NOTE}")

    client, how = await asyncio.to_thread(_lookup, email, account)
    if not client:
        await asyncio.to_thread(record, "Failed", notes="email/account not found")
        await log(f"⚠️ {member.mention} — no match for `{email}`"
                  + (f" / account `{account}`" if account else ""))
        return False, (
            "We couldn't find an account with that email address yet.\n\n"
            "If you registered with us recently, your details may not have "
            "reached us — we'll keep checking, and your role will be assigned "
            "automatically within a few hours. You don't need to do anything.\n\n"
            "If you registered a while ago, please double-check it's the same "
            f"address you used at Acme Trading. {SUPPORT_NOTE}")

    if how == "account":
        await log(f"⚠️ {member.mention} matched by **account number** `{account}` "
                  f"→ `{client['email']}` (the email they typed was `{email}`)")

    if client["is_blacklisted"]:
        # recorded for visibility only - roles are still assigned normally
        await log(f"ℹ️ {member.mention} → `{client['email']}` is flagged as "
                  f"blacklisted in the portal (roles assigned as normal)")

    tier = client["highest_tier"]
    is_pc = bool(client.get("is_inner_circle"))
    old_tier = existing["current_tier"] if existing else None
    prev_email = existing["email"] if existing else None
    relinked = prev_email is not None and prev_email != client["email"]
    changed = old_tier != tier

    try:
        added, removed = await apply_roles(member, tier)
    except discord.Forbidden:
        await asyncio.to_thread(record, "Failed", tier=tier, notes="missing permissions")
        return False, ("Something went wrong assigning your roles. "
                       f"{SUPPORT_NOTE}")

    await asyncio.to_thread(_save_link, uid, client["email"],
                            client["customer_number"], tier, int(is_pc))
    await asyncio.to_thread(record, "Done", tier, stack_names(tier),
                            f"matched by {how}, source={source}",
                            client["customer_number"])

    if relinked:
        await log(f"🔗 {member.mention} re-linked `{prev_email}` → "
                  f"`{client['email']}` (**{TIER_LABEL[tier]}**)")

    if changed:
        await queue_announcement(member, tier, old_tier)
        await log(f"✅ {member.mention} → `{client['email']}` → **{TIER_LABEL[tier]}** "
                  f"({stack_names(tier)})"
                  + (f" [was {TIER_LABEL.get(old_tier, old_tier)}]" if old_tier else ""))
    else:
        await log(f"↩️ {member.mention} re-verified, still **{TIER_LABEL[tier]}** "
                  f"(no announcement)")

    if relinked:
        msg = (f"Your Discord account is now linked to **{client['email']}**"
               f" (was `{prev_email}`).\n"
               f"Tier: **{TIER_LABEL[tier]}**\n"
               f"Roles: {stack_names(tier)}")
    elif changed and old_tier:
        msg = (f"Your role has been upgraded to **{TIER_LABEL[tier]}**.\n"
               f"You now have: {stack_names(tier)}")
    elif changed:
        msg = (f"You're verified as **{TIER_LABEL[tier]}**.\n"
               f"Roles assigned: {stack_names(tier)}")
    else:
        msg = (f"You're already verified as **{TIER_LABEL[tier]}**.\n"
               f"Your roles: {stack_names(tier)}\n\n"
               "If you've recently passed a challenge or received a payout, it can "
               "take a couple of hours to show up here.")
    return True, msg


# ----------------------------------------------------------------------
# modal + persistent button
# ----------------------------------------------------------------------
class VerifyModal(discord.ui.Modal, title="Verify your Acme Trading account"):
    email = discord.ui.TextInput(
        label="Email address",
        placeholder="the email you registered with at Acme Trading",
        required=True, max_length=200)
    account = discord.ui.TextInput(
        label="Account number (optional)",
        placeholder="e.g. 2200342", required=False, max_length=50)
    twitter = discord.ui.TextInput(
        label="Twitter / X profile link (optional)",
        placeholder="https://x.com/yourhandle  —  full link, not a username",
        required=False, max_length=200)
    instagram = discord.ui.TextInput(
        label="Instagram profile link (optional)",
        placeholder="https://instagram.com/yourhandle  —  full link, not a username",
        required=False, max_length=200)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await verify_member(
            interaction.user, str(self.email), str(self.account),
            str(self.twitter), str(self.instagram), source="button")
        await interaction.followup.send(
            ("✅ " if ok else "❌ ") + msg, ephemeral=True)


class VerifyView(discord.ui.View):
    """timeout=None + a fixed custom_id makes the button survive restarts."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim my trader role", style=discord.ButtonStyle.primary,
                       emoji="🎫", custom_id="tierbot:verify:v1")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VerifyModal())


# ----------------------------------------------------------------------
# slash commands
# ----------------------------------------------------------------------
def is_admin():
    async def pred(interaction: discord.Interaction):
        return interaction.user.guild_permissions.manage_roles
    return app_commands.check(pred)


@bot.tree.command(name="refresh", description="Re-check your Acme Trading roles")
async def refresh(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    link = await asyncio.to_thread(_get_link, interaction.user.id)
    if not link:
        await interaction.followup.send(
            "❌ You haven't verified yet. Use the Verify button first.", ephemeral=True)
        return
    ok, msg = await verify_member(interaction.user, link["email"], source="refresh")
    await interaction.followup.send(("✅ " if ok else "❌ ") + msg, ephemeral=True)


@bot.tree.command(name="postverify", description="Post the Verify button (admin)")
@is_admin()
async def postverify(interaction: discord.Interaction):
    # Acknowledge first: Discord allows only 3 seconds, and sending the message
    # can fail if the bot lacks Send Messages in this channel.
    await interaction.response.defer(ephemeral=True, thinking=True)
    embed = discord.Embed(
        title="🎫 Claim Your Trader Role",
        description=(
            "This will unlock access to Channels, Exclusive Giveaways, "
            "Offers & more.\n\n"
            "Every step of your journey with Acme Trading earns you a place here.\n\n"
            "👑  **Inner Circle**  ·  🏆  **Payout**  ·  📈  **Funded**  ·  "
            "🎯  **Challenge**\n\n"
            "Press the button below and enter the email address you registered "
            "with us. We'll find your account and hand you the role you've "
            "earned — along with access to the channels that come with it.\n\n"
            "*Takes a few seconds. Only you can see what you submit.*"),
        colour=discord.Colour.blurple())
    embed.set_author(name="VERIFY YOUR ACCOUNT")
    try:
        await interaction.channel.send(embed=embed, view=VerifyView())
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ I don't have permission to post in this channel.\n"
            "Give the bot's role **Send Messages** and **Embed Links** here "
            "(Edit Channel → Permissions → add the bot's role).", ephemeral=True)
        return
    await interaction.followup.send("✅ Posted.", ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    """Without this, a failed permission check leaves the interaction hanging
    and Discord shows 'The application did not respond'."""
    if isinstance(error, app_commands.CheckFailure):
        msg = ("You need the **Manage Roles** permission to use this command.")
    elif isinstance(error, discord.Forbidden):
        msg = ("I'm missing a permission for that. Check that my role sits "
               "above the five assignable roles and that I can post here.")
    else:
        msg = f"Something went wrong: `{type(error).__name__}: {error}`"

    print(f"app command error: {type(error).__name__}: {error}", flush=True)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ {msg}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)
    except discord.HTTPException:
        pass


@bot.tree.command(name="checkroles", description="Check the bot's role hierarchy (admin)")
@is_admin()
async def checkroles(interaction: discord.Interaction):
    g = interaction.guild
    me = g.me
    bot_top = me.top_role
    lines = [f"My highest role: **{bot_top.name}** (position {bot_top.position})",
             f"Manage Roles permission: "
             f"{'yes' if me.guild_permissions.manage_roles else 'NO'}", ""]
    problems = 0
    for key, rid in ROLE_IDS.items():
        if not rid:
            lines.append(f"⚪ {TIER_LABEL.get(key, key)} — not configured")
            continue
        role = g.get_role(rid)
        if not role:
            lines.append(f"❌ {TIER_LABEL.get(key, key)} — role ID {rid} not found")
            problems += 1
            continue
        if role >= bot_top:
            lines.append(f"❌ {role.name} (position {role.position}) is at or "
                         f"ABOVE my role — move it down, or move me up")
            problems += 1
        else:
            lines.append(f"✅ {role.name} (position {role.position})")
    lines.append("")
    lines.append("All good." if not problems
                 else f"**{problems} problem(s)** — drag my role above them in "
                      f"Server Settings → Roles.")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(
    name="migrate_verified",
    description="Move bot-verified members off the old Verified Role (admin)")
@is_admin()
@app_commands.describe(
    confirm="Type YES to actually make the change. Leave blank for a dry run.")
async def migrate_verified(interaction: discord.Interaction, confirm: str = ""):
    """
    One-off. Removes the old Verified Role from members who claimed through
    this bot, leaving everyone who only reacted in the welcome channel alone.

    Membership of discord_links is the only safe way to tell the two apart -
    Discord itself cannot distinguish how someone came by a role.
    """
    await interaction.response.defer(ephemeral=True, thinking=True)

    if not OLD_VERIFIED_ROLE_ID:
        await interaction.followup.send(
            "OLD_VERIFIED_ROLE_ID is not set at the top of bot.py.",
            ephemeral=True)
        return

    g = interaction.guild
    old_role = g.get_role(OLD_VERIFIED_ROLE_ID)
    if old_role is None:
        await interaction.followup.send(
            f"No role with ID {OLD_VERIFIED_ROLE_ID} in this server.",
            ephemeral=True)
        return
    if old_role >= g.me.top_role:
        await interaction.followup.send(
            f"**{old_role.name}** sits at or above my own role, so I can't "
            f"change it. Move my role above it first.", ephemeral=True)
        return

    links = await asyncio.to_thread(_all_links)
    targets, absent, gone = [], 0, 0
    for link in links:
        member = g.get_member(int(link["discord_user_id"]))
        if not member:
            gone += 1
            continue
        if old_role in member.roles:
            targets.append(member)
        else:
            absent += 1

    dry = confirm.strip().upper() != "YES"
    if dry:
        await interaction.followup.send(
            f"**Dry run — nothing changed.**\n"
            f"Verified through the bot: **{len(links)}**\n"
            f"Still in the server: **{len(links) - gone}**\n"
            f"Holding **{old_role.name}**: **{len(targets)}** ← would be removed\n"
            f"Already without it: **{absent}**\n"
            f"Left the server: **{gone}**\n\n"
            f"{old_role.name} has **{len(old_role.members):,}** members in total; "
            f"the other **{len(old_role.members) - len(targets):,}** are untouched.\n\n"
            f"Run `/migrate_verified confirm:YES` to apply.", ephemeral=True)
        return

    done, failed = 0, 0
    for member in targets:
        try:
            await member.remove_roles(
                old_role, reason="Moved to Verified Member")
            done += 1
        except discord.HTTPException:
            failed += 1
        await asyncio.sleep(0.4)          # stay inside the rate limit

    await interaction.followup.send(
        f"Removed **{old_role.name}** from **{done}** member(s)."
        + (f" {failed} failed." if failed else ""), ephemeral=True)
    await log(f"🔀 {interaction.user.mention} moved **{done}** member(s) off "
              f"**{old_role.name}**")


@bot.tree.command(
    name="diagnose",
    description="Test each role individually on a member to find the blocker (admin)")
@is_admin()
async def diagnose(interaction: discord.Interaction, member: discord.Member):
    """
    Adds each assignable role to the member one at a time and reports which
    ones Discord refuses. Guessing from a combined failure tells us nothing -
    this narrows it to the specific role and the specific error code.
    """
    await interaction.response.defer(ephemeral=True, thinking=True)
    g = interaction.guild
    me = g.me
    bot_top = me.top_role

    out = [
        f"**Bot:** {bot_top.name} · position **{bot_top.position}** · "
        f"Manage Roles: **{'yes' if me.guild_permissions.manage_roles else 'NO'}** · "
        f"Admin: {'yes' if me.guild_permissions.administrator else 'no'}",
        f"**Member:** {member.display_name} · highest role "
        f"**{member.top_role.name}** (position {member.top_role.position})"
        + ("  ⛔ SERVER OWNER" if member.id == g.owner_id else ""),
        "",
        "**Their roles:**",
    ]
    for r in sorted(member.roles, key=lambda x: -x.position):
        marks = []
        if r.position >= bot_top.position and r.name != "@everyone":
            marks.append("at/above me")
        if r.managed:
            marks.append("managed")
        out.append(f"  • {r.name} ({r.position})"
                   + (f"  ⛔ {', '.join(marks)}" if marks else ""))

    out.append("")
    out.append("**Server / app state:**")
    try:
        out.append(f"  MFA requirement: `{g.mfa_level}` "
                   f"(if 2FA is required and the app owner has none, all "
                   f"moderation fails)")
    except Exception:                                       # noqa: BLE001
        pass
    out.append(f"  member pending screening: "
               f"**{getattr(member, 'pending', 'unknown')}** "
               f"(a member who has not accepted the rules cannot be given roles)")
    out.append(f"  guild id seen by bot: `{g.id}` · configured: `{GUILD_ID}` · "
               f"{'match' if g.id == GUILD_ID else '**MISMATCH**'}")
    perms = me.guild_permissions
    on = [n for n, v in perms if v]
    out.append(f"  my permissions ({len(on)}): {', '.join(on[:14])}"
               + (" ..." if len(on) > 14 else ""))
    managed = [g.get_role(r).name for r in ROLE_IDS.values()
               if r and g.get_role(r) and g.get_role(r).managed]
    out.append(f"  managed roles among mine: "
               f"{', '.join(managed) if managed else 'none'} "
               f"(a managed role cannot be assigned by anyone)")

    out.append("")
    out.append("**Testing each role on its own:**")
    had = {r.id for r in member.roles}
    added_ok = []
    for key, rid in ROLE_IDS.items():
        if not rid:
            out.append(f"  ⚪ {key} — not configured")
            continue
        role = g.get_role(rid)
        if role is None:
            out.append(f"  ❌ {key} — role ID `{rid}` does not exist in this server")
            continue
        if rid in had:
            out.append(f"  ⏭️ {role.name} — they already have it")
            continue
        try:
            await member.add_roles(role, reason="diagnose")
            out.append(f"  ✅ {role.name} (pos {role.position}) — assigned")
            added_ok.append(role)
        except discord.Forbidden as e:
            out.append(f"  ❌ {role.name} (pos {role.position}) — "
                       f"`{getattr(e, 'text', e)}` code **{getattr(e, 'code', '?')}**")
        except discord.HTTPException as e:
            out.append(f"  ❌ {role.name} (pos {role.position}) — HTTP "
                       f"{getattr(e, 'status', '?')} `{getattr(e, 'text', e)}`")
        await asyncio.sleep(0.3)

    # put them back the way we found them
    if added_ok:
        try:
            await member.remove_roles(*added_ok, reason="diagnose cleanup")
            out.append("")
            out.append(f"_(removed the {len(added_ok)} test role(s) again)_")
        except discord.HTTPException:
            out.append("")
            out.append("_(could not remove the test roles - please do it by hand)_")

    await interaction.followup.send("\n".join(out)[:1990], ephemeral=True)


@bot.tree.command(
    name="pending",
    description="Show claims that failed and have not resolved yet (admin)")
@is_admin()
async def pending_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    g = interaction.guild
    rows = await asyncio.to_thread(_pending_retries, RETRY_FAILED_DAYS)
    if not rows:
        await interaction.followup.send(
            "Nothing outstanding — every claim has resolved.", ephemeral=True)
        return

    groups = {"not in the data": [], "claimed by another account": [],
              "left the server": [], "ready to assign": []}
    for row in rows:
        uid = row["discord_user_id"]
        member = g.get_member(int(uid))
        if not member:
            groups["left the server"].append((row, None))
            continue
        client = await asyncio.to_thread(_tier_for_email, row["email"])
        if not client:
            groups["not in the data"].append((row, member))
            continue
        owner = await asyncio.to_thread(_link_owner, row["email"])
        if owner and owner != uid:
            groups["claimed by another account"].append((row, member, owner))
            continue
        groups["ready to assign"].append((row, member))

    out = [f"**{len(rows)} unresolved claim(s)** in the last "
           f"{RETRY_FAILED_DAYS} days\n"]

    g1 = groups["not in the data"]
    if g1:
        out.append(f"__**{len(g1)} — email not in our client data**__")
        out.append("They registered after the last data refresh, or used a "
                   "different address. Download the reports and rebuild, then "
                   "`/syncall`.")
        for row, member in g1[:12]:
            out.append(f"• {member.mention} — `{row['email']}`")
        if len(g1) > 12:
            out.append(f"…and {len(g1) - 12} more")
        out.append("")

    g2 = groups["claimed by another account"]
    if g2:
        out.append(f"__**{len(g2)} — email already linked elsewhere**__")
        out.append("That email is linked to a different Discord account. Either "
                   "the person has two accounts, or someone entered an address "
                   "that is not theirs. Check with `/whois` on both, then "
                   "`/unlink` whichever is wrong.")
        for row, member, owner in g2[:12]:
            out.append(f"• {member.mention} tried `{row['email']}` — held by "
                       f"<@{owner}>")
        out.append("")

    g3 = groups["ready to assign"]
    if g3:
        out.append(f"__**{len(g3)} — ready**__ (run `/syncall`)")
        for row, member in g3[:12]:
            out.append(f"• {member.mention} — `{row['email']}`")
        out.append("")

    g4 = groups["left the server"]
    if g4:
        out.append(f"__**{len(g4)} — no longer in the server**__ "
                   f"(nothing to do)")

    await interaction.followup.send("\n".join(out)[:1990], ephemeral=True)


@bot.tree.command(name="lookup", description="Look up a client by email (admin)")
@is_admin()
@app_commands.describe(email="Client email address")
async def lookup(interaction: discord.Interaction, email: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    row = await asyncio.to_thread(_tier_for_email, email.strip().lower())
    if not row:
        await interaction.followup.send("No client with that email.", ephemeral=True)
        return
    await interaction.followup.send(
        f"**{email}**\ncustomer: `{row['customer_number']}`\n"
        f"tier: **{TIER_LABEL.get(row['highest_tier'], row['highest_tier'])}**\n"
        f"inner circle: {'yes' if row.get('is_inner_circle') else 'no'}\n"
        f"blacklisted: {'yes' if row['is_blacklisted'] else 'no'}", ephemeral=True)


@bot.tree.command(name="whois", description="Show which email a member is linked to (admin)")
@is_admin()
async def whois(interaction: discord.Interaction, member: discord.Member):
    link = await asyncio.to_thread(_get_link, member.id)
    if not link:
        await interaction.response.send_message("Not verified.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"{member.mention}\nemail: `{link['email']}`\n"
        f"customer: `{link['customer_number']}`\n"
        f"tier: **{TIER_LABEL.get(link['current_tier'], link['current_tier'])}**\n"
        f"inner circle: {'yes' if link.get('is_inner_circle') else 'no'}\n"
        f"verified: {link['verified_at']}", ephemeral=True)


@bot.tree.command(name="unlink", description="Unlink a member so they can verify again (admin)")
@is_admin()
async def unlink(interaction: discord.Interaction, member: discord.Member):
    n = await asyncio.to_thread(_unlink, member.id)
    await interaction.response.send_message(
        f"Unlinked {member.mention}." if n else "That member wasn't linked.",
        ephemeral=True)
    if n:
        await log(f"🔓 {interaction.user.mention} unlinked {member.mention}")


@bot.tree.command(name="override", description="Force a member's tier (admin)")
@is_admin()
@app_commands.choices(tier=[
    app_commands.Choice(name=v, value=k) for k, v in TIER_LABEL.items()])
async def override(interaction: discord.Interaction, member: discord.Member,
                   tier: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True, thinking=True)
    await apply_roles(member, tier.value)
    await interaction.followup.send(
        f"Set {member.mention} to **{tier.name}**.", ephemeral=True)
    await log(f"🔧 {interaction.user.mention} overrode {member.mention} "
              f"→ **{tier.name}**")


@bot.tree.command(name="announcements", description="Turn announcements on or off (admin)")
@is_admin()
@app_commands.choices(state=[
    app_commands.Choice(name="on", value="on"),
    app_commands.Choice(name="off", value="off")])
async def announcements(interaction: discord.Interaction,
                        state: app_commands.Choice[str]):
    global announcements_on
    announcements_on = state.value == "on"
    await interaction.response.send_message(
        f"Announcements are now **{state.value}**.", ephemeral=True)
    await log(f"📢 {interaction.user.mention} turned announcements **{state.value}**")


@bot.tree.command(name="syncall", description="Re-check every verified member now (admin)")
@is_admin()
async def syncall(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    changed = await run_sync()
    pending = await asyncio.to_thread(_pending_retries, RETRY_FAILED_DAYS)
    retried = await retry_failed()
    linked = len(await asyncio.to_thread(_all_links))
    await interaction.followup.send(
        f"Sync complete.\n"
        f"• verified members on file: **{linked}**\n"
        f"• roles updated: **{changed}**\n"
        f"• earlier failed claims retried: **{len(pending)}**, "
        f"of which **{retried}** now matched", ephemeral=True)


# ----------------------------------------------------------------------
# background sync  -  upgrades without anyone filling the form again
# ----------------------------------------------------------------------
async def run_sync():
    g = guild()
    if not g:
        return 0
    changed = 0
    not_in_server = 0
    for link in await asyncio.to_thread(_all_links):
        member = g.get_member(int(link["discord_user_id"]))
        if not member:
            not_in_server += 1
            continue
        row = await asyncio.to_thread(_tier_for_email, link["email"])
        if not row:
            continue
        new_tier = row["highest_tier"]
        new_pc = bool(row.get("is_inner_circle"))
        old_tier = link["current_tier"]

        # Do not skip purely because the tier is unchanged. The member may
        # still be missing a role - after a new role is added to the stack,
        # for example - so check what they actually hold.
        want = {ROLE_IDS[k] for k in TIER_STACK.get(new_tier, []) if ROLE_IDS.get(k)}
        want -= {rid for rid in PROTECTED_ROLE_IDS if rid}
        has = {r.id for r in member.roles}
        roles_ok = want.issubset(has)

        if new_tier == old_tier and roles_ok:
            continue
        try:
            await apply_roles(member, new_tier)
        except discord.Forbidden:
            continue
        except discord.HTTPException as e:
            await log(f"⚠️ could not update {member.mention}: {e}")
            continue
        await asyncio.to_thread(_save_link, member.id, link["email"],
                                row["customer_number"], new_tier, int(new_pc))
        rose = TIER_RANK.get(new_tier, 0) > TIER_RANK.get(old_tier, 0)
        if new_tier == old_tier:
            # roles were out of step with the tier - repaired, no announcement
            await log(f"🔧 {member.mention} roles repaired → "
                      f"**{TIER_LABEL.get(new_tier, new_tier)}**")
        elif rose:
            await queue_announcement(member, new_tier, old_tier)
            await log(f"🔄 {member.mention} upgraded "
                      f"**{TIER_LABEL.get(old_tier, old_tier)} → {TIER_LABEL[new_tier]}**")
        else:
            note = ("removed from the Inner Circle sheet"
                    if old_tier == "inner_circle"
                    else "check why — this should not happen")
            await log(f"⬇️ {member.mention} moved "
                      f"**{TIER_LABEL.get(old_tier, old_tier)} → {TIER_LABEL[new_tier]}** "
                      f"({note})")
        changed += 1
        await asyncio.sleep(0.5)          # stay well inside the API rate limit
    return changed


async def retry_failed():
    """Finish off claims that failed only because the data was not there yet."""
    g = guild()
    if not g:
        return 0
    pending = await asyncio.to_thread(_pending_retries, RETRY_FAILED_DAYS)
    fixed = 0
    skipped = {"not in server": 0, "email still not in the data": 0,
               "email claimed by someone else": 0, "could not assign roles": 0}
    for row in pending:
        member = g.get_member(int(row["discord_user_id"]))
        if not member:
            skipped["not in server"] += 1
            continue
        email = row["email"]

        client = await asyncio.to_thread(_tier_for_email, email)
        if not client:
            skipped["email still not in the data"] += 1
            continue

        owner = await asyncio.to_thread(_link_owner, email)
        if owner and owner != str(member.id):
            skipped["email claimed by someone else"] += 1
            continue

        tier = client["highest_tier"]
        try:
            await apply_roles(member, tier)
        except discord.Forbidden:
            skipped["could not assign roles"] += 1
            continue
        except discord.HTTPException as e:
            skipped["could not assign roles"] += 1
            await log(f"⚠️ could not update {member.mention}: {e}")
            continue

        await asyncio.to_thread(_save_link, member.id, email,
                                client["customer_number"], tier,
                                int(client.get("is_inner_circle") or 0))
        await asyncio.to_thread(
            _log_submission, ts=now(), discord_user_id=str(member.id),
            discord_username=str(member), email=email, account_number="",
            twitter="", instagram="", matched_tier=tier,
            roles_assigned=stack_names(tier), status="Done",
            notes="matched on retry after a data refresh")

        if sheets_sync is not None and sheets_sync.enabled():
            try:
                sheets_sync.append_submission({
                    "discord_user_id": str(member.id),
                    "discord_username": str(member), "email": email,
                    "account_number": "", "twitter": "", "instagram": "",
                    "customer_number": client["customer_number"],
                    "matched_tier": tier, "roles_assigned": stack_names(tier),
                    "status": "Done",
                    "notes": "matched on retry after a data refresh"})
            except Exception as e:                          # noqa: BLE001
                print(f"sheets mirror failed: {e}", flush=True)

        await queue_announcement(member, tier, None)
        await log(f"🔁 {member.mention} matched on retry → "
                  f"**{TIER_LABEL[tier]}** (`{email}`)")
        fixed += 1
        await asyncio.sleep(0.5)

    # Only speak up when something actually happened. Reporting the same
    # unchanged backlog on every restart reads like a fault when it is not -
    # use /pending to see the detail on demand.
    if fixed:
        reasons = ", ".join(f"{v} {k}" for k, v in skipped.items() if v)
        await log(f"🔁 retry: **{fixed}** earlier claim(s) now matched"
                  + (f" · still waiting: {reasons}" if reasons else ""))
    return fixed


_db_mtime = {"seen": None}


@tasks.loop(seconds=WATCH_DB_SECONDS)
async def watch_db():
    """
    Sync as soon as the database changes.

    build_journey.py rewrites client_journey.db, so its modification time is a
    reliable signal that there is new data. Without this, a rebuild sits unused
    until the next scheduled cycle - which is why restarting the bot appeared
    to be what applied the updates.
    """
    try:
        mtime = os.path.getmtime(DB_PATH)
    except OSError:
        return

    if _db_mtime["seen"] is None:                 # first run - just record it
        _db_mtime["seen"] = mtime
        return
    if mtime == _db_mtime["seen"]:
        return

    # let the builder finish writing before reading
    await asyncio.sleep(5)
    try:
        mtime = os.path.getmtime(DB_PATH)
    except OSError:
        return
    _db_mtime["seen"] = mtime

    await log("📥 New client data detected — syncing roles")
    changed = await run_sync()
    matched = await retry_failed()
    if changed or matched:
        await log(f"📥 Sync after rebuild: **{changed}** role update(s), "
                  f"**{matched}** earlier claim(s) matched")
    else:
        await log("📥 Sync after rebuild: nothing to change")


@watch_db.before_loop
async def before_watch():
    await bot.wait_until_ready()


@watch_db.error
async def watch_db_error(error):
    print(f"watch_db crashed: {type(error).__name__}: {error}", flush=True)
    await asyncio.sleep(60)
    if not watch_db.is_running():
        watch_db.restart()


@tasks.loop(hours=SYNC_INTERVAL_HOURS)
async def sync_loop():
    n = await run_sync()
    if n:
        await log(f"🔄 Scheduled sync: {n} member(s) updated.")
    await retry_failed()


@sync_loop.before_loop
async def before_sync():
    await bot.wait_until_ready()


@sync_loop.error
async def sync_loop_error(error):
    """
    Without this, one unhandled exception stops the loop permanently and
    nobody is upgraded again until the bot is restarted - silently.
    """
    print(f"sync_loop crashed: {type(error).__name__}: {error}", flush=True)
    await log(f"⚠️ The scheduled sync hit an error and is restarting: "
              f"`{type(error).__name__}`")
    notify(f"⚠️ Acme Trading role bot: the scheduled sync crashed "
           f"({type(error).__name__}: {error}) and is restarting.",
           key="sync-crash")
    await asyncio.sleep(60)
    if not sync_loop.is_running():
        sync_loop.restart()


# ----------------------------------------------------------------------
# events
# ----------------------------------------------------------------------
@bot.event
async def on_ready():
    print(f"logged in as {bot.user} ({bot.user.id})", flush=True)
    g = guild()
    if g:
        cached, total = len(g.members), (g.member_count or 0)
        print(f"guild {g.name}: {cached} members cached of {total}", flush=True)
        if total and cached < total * 0.5:
            msg = (f"I can only see {cached} of {total} members. Server Members "
                   f"Intent is probably switched off in the Developer Portal - "
                   f"without it roles cannot be assigned.")
            await log(f"⚠️ {msg}")
            notify(f"⚠️ Acme Trading role bot: {msg}", key="members-intent")
    await log(f"🟢 Bot online — announcements "
              f"**{'on' if announcements_on else 'off'}**")


@bot.event
async def on_member_join(member):
    """Someone who verified before, left, and came back: restore silently."""
    if member.guild.id != GUILD_ID:
        return
    link = await asyncio.to_thread(_get_link, member.id)
    if not link:
        return
    row = await asyncio.to_thread(_tier_for_email, link["email"])
    if not row:
        return
    tier = row["highest_tier"]
    try:
        await apply_roles(member, tier)
        await log(f"↩️ {member.mention} rejoined — restored **{TIER_LABEL[tier]}**")
    except discord.HTTPException:
        pass


@bot.event
async def setup_hook():
    init_db()
    bot.add_view(VerifyView())                      # makes the button persistent
    try:
        guild_obj = discord.Object(id=GUILD_ID)
        # Commands are declared globally, so copy them into the guild before
        # syncing. A guild sync appears instantly; a global sync can take an
        # hour to propagate.
        bot.tree.copy_global_to(guild=guild_obj)
        synced = await bot.tree.sync(guild=guild_obj)
        print(f"synced {len(synced)} slash commands to guild {GUILD_ID}: "
              f"{sorted(c.name for c in synced)}", flush=True)
    except discord.Forbidden:
        print("SYNC FAILED (403). The bot was invited without the "
              "'applications.commands' scope. Re-invite it with both 'bot' and "
              "'applications.commands' selected.", flush=True)
    except discord.HTTPException as e:
        print(f"SYNC FAILED: {e}", flush=True)
    bot.loop.create_task(announcement_worker())
    sync_loop.start()
    watch_db.start()


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN not set. Put it in a .env file.")
    required_channels = {k: v for k, v in CHANNEL_IDS.items() if k != "logs"}
    if not GUILD_ID or not all(ROLE_IDS.values()) or not all(required_channels.values()):
        raise SystemExit("Fill in GUILD_ID, ROLE_IDS and CHANNEL_IDS "
                         "(verify, winners) at the top of this file first.")
    if not CHANNEL_IDS.get("logs"):
        print("note: no logs channel set - logging to console only", flush=True)
    bot.run(token)


if __name__ == "__main__":
    main()
