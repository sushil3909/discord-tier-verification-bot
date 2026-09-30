# Discord Tier Verification Bot

A Discord bot that verifies members against a client database and assigns tiered roles
automatically. A member presses a button, enters the email they registered with, and gets
the roles their account history has earned. A background job keeps those roles up to date
as the data changes.

> This is a sanitized version of an internal tool I built for a proprietary trading firm.
> The brand name (`Acme Trading`) is a placeholder, all server, role and channel IDs are
> zeroed, and no client data or credentials are included. The script that downloaded the
> reports from the firm's admin portal is left out because it is specific to that portal.

## How it works

```
four CSV exports  ->  build_journey.py (pandas)  ->  client_journey.db (SQLite)
                                                           |
Discord member  ->  Verify button + form  ->  bot.py  ->  roles assigned
                                                           |
                                        sheets_sync.py  ->  Google Sheet mirror (optional)
```

## Tiers

Roles are stacked: each tier grants itself and everything below it.

| Tier | Earned by |
|---|---|
| Inner Circle | Listed in a curated sheet maintained by the team |
| Payout | Any completed payout |
| Funded | Holds or ever held a funded account |
| Challenge | Passed a challenge stage but never reached a funded account |
| Account Holder | Bought an account, never passed a stage |
| Verified | Registered, nothing else |

## Design notes

**Achievement flags only move forward.** `ever_challenge`, `ever_funded`, `ever_payout` and
Inner Circle membership are merged with `MAX()` in the upsert, so they can go from 0 to 1 and
never back. A failed, partial or truncated export can add nothing, but it can never remove a
role someone earned. That is what makes the build safe to run unattended.

**Background reconciler.** A loop re-checks every linked member every four hours, and a
second loop watches the database file and syncs within a minute of a rebuild. It compares the
roles a member actually holds against what their tier should grant, so a missing role is
repaired even when the tier has not changed.

**Retry queue.** A new client can try to verify before their record reaches the database.
Those failed claims are kept for seven days and retried on every sync, so the role arrives
without the member filling in the form again.

**Announcements without rate-limit stalls.** Role announcements go through a queue drained by
one worker at a fixed interval, so a busy hour never makes verification wait on Discord's
channel rate limit.

**Sheets mirror that cannot break verification.** SQLite is the source of truth. Every
submission is written there first and mirrored to a Google Sheet second; if Google is slow or
down, the row is picked up by the next reconcile. Rows are keyed on (timestamp, Discord user
ID), so the mirror can be re-run without creating duplicates.

**Alerts for real problems only.** Faults that need a person, such as a role the bot cannot
assign or a crashed sync loop, go to a Google Chat webhook with a cooldown per fault type.

## Slash commands

| Command | Who | Purpose |
|---|---|---|
| `/refresh` | Member | Re-check my roles |
| `/postverify` | Admin | Post the Verify button |
| `/checkroles` | Admin | Check the bot's position in the role hierarchy |
| `/diagnose` | Admin | Test each role on one member to find what blocks assignment |
| `/pending` | Admin | Show failed claims that have not resolved yet |
| `/lookup` | Admin | Look up a client by email |
| `/whois` | Admin | Show which email a member is linked to |
| `/unlink` | Admin | Unlink a member so they can verify again |
| `/override` | Admin | Force a member's tier |
| `/announcements` | Admin | Turn announcements on or off |
| `/syncall` | Admin | Re-check every verified member now |
| `/migrate_verified` | Admin | One-off move from an older verification role, with a dry run |

## Try the data pipeline with sample data

No real data is needed to see the build work:

```
pip install -r requirements.txt
python make_sample_data.py     # writes four synthetic CSV exports
python build_journey.py        # builds client_journey.db and prints the tier breakdown
```

## Running the bot

1. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`
2. Fill in `GUILD_ID`, `ROLE_IDS` and `CHANNEL_IDS` at the top of `bot.py`
3. Enable the Server Members Intent for the bot in the Discord Developer Portal
4. Place the bot's role above the roles it assigns
5. `python bot.py`, then run `/postverify` once in the verification channel

The Google Sheets mirror is optional: set `GSHEET_ID` and add a service-account key as
`service_account.json` to enable it. `python sheets_sync.py check` reports what is configured.

## Stack

Python, discord.py, pandas, SQLite, gspread / Google Sheets API, Google Chat webhooks
