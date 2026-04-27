import discord
from discord import app_commands
from discord.ext import commands, tasks
import gspread
from google.oauth2.service_account import Credentials
import aiohttp
import requests
import os
from dotenv import load_dotenv
import asyncio
from datetime import datetime, timedelta
import json
from flask import Flask
from threading import Thread
from waitress import serve
from collections import defaultdict
import time as time_module
import functools
import sys
import re
sys.stdout.reconfigure(line_buffering=True)

# Simple web server to keep Render happy
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    serve(app, host='0.0.0.0', port=10000)

def keep_alive():
    t = Thread(target=run_flask)
    t.start()


load_dotenv()


# -------------------------------------------------
#  RATE LIMITING
# -------------------------------------------------
_user_cooldowns: dict[int, dict[str, float]] = defaultdict(dict)

COMMAND_COOLDOWNS: dict[str, float] = {
    "bookacademy":          10,
    "booksf":               10,
    "removebookingacademy": 10,
    "removebookingsf":      10,
    "edit":                 15,
    "hire":                 30,
    "removestaff":          30,
    "strike":               15,
    "revokestrike":         15,
    "loa":                  15,
    "endloa":               15,
    "logtraining":          15,
    "attendance":           10,
    "resetattendance":      60,
    "postacademy":          30,
    "postsixthform":        30,
    "info":                  5,
    "profile":               5,
    "checkattendance":      10,
    "status":                5,
    "ping":                  3,
    "hello":                 3,
    "setstatus":            10,
    "diagnoseroblox":       30,
    "openroles":            10,
    "viewstaff":            10,
    "requestname":          30,
    "requestdisplayname":   100,
    "staffblacklist":       15,
    "unblackliststaff":     15,
    "syncstaff":            15,
}

def cooldown(command_name: str | None = None):
    """Decorator — blocks reuse until cooldown expires."""
    def decorator(func):
        cmd_name = command_name

        @functools.wraps(func)
        async def wrapper(interaction: discord.Interaction, *args, **kwargs):
            name = cmd_name or interaction.command.name
            limit = COMMAND_COOLDOWNS.get(name, 5)
            user_id = interaction.user.id

            elapsed = time_module.monotonic() - _user_cooldowns[user_id].get(name, 0.0)
            remaining = limit - elapsed

            if remaining > 0:
                msg = f"⏳ Please wait **{remaining:.1f}s** before using `/{name}` again."
                try:
                    await interaction.response.send_message(msg, ephemeral=True)
                except discord.InteractionResponded:
                    await interaction.followup.send(msg, ephemeral=True)
                return

            _user_cooldowns[user_id][name] = time_module.monotonic()
            await func(interaction, *args, **kwargs)

        return wrapper
    return decorator


# -------------------------------------------------
#  SAFE HTTP (Apps Script) — exponential back-off
# -------------------------------------------------
_http_semaphore = asyncio.Semaphore(3)

_http_session: aiohttp.ClientSession | None = None

async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


async def safe_apps_script_get(
    url: str,
    params: dict,
    *,
    retries: int = 4,
    base_delay: float = 1.0,
) -> tuple[int, str]:
    delay = base_delay
    last_status, last_text = 0, "no response"

    async with _http_semaphore:
        for attempt in range(retries):
            try:
                session = await get_http_session()
                async with session.get(
                    url, params=params, allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    last_status = resp.status
                    last_text = await resp.text()

                    if last_status == 429:
                        wait = float(resp.headers.get("Retry-After", delay))
                        print(f"[HTTP] 429 rate-limited (attempt {attempt+1}). Waiting {wait:.1f}s…")
                        await asyncio.sleep(wait)
                        delay *= 2
                        continue

                    if last_status >= 500:
                        print(f"[HTTP] {last_status} server error (attempt {attempt+1}). Retrying in {delay:.1f}s…")
                        await asyncio.sleep(delay)
                        delay *= 2
                        continue

                    return last_status, last_text

            except aiohttp.ClientError as exc:
                print(f"[HTTP] Network error (attempt {attempt+1}): {exc}")
                if attempt < retries - 1:
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    raise

    return last_status, last_text


# -------------------------------------------------
#  SAFE SHEETS — quota-aware back-off wrapper
# -------------------------------------------------
_sheets_semaphore = asyncio.Semaphore(5)

async def safe_sheets_call(fn, *, retries: int = 5, base_delay: float = 2.0):
    delay = base_delay
    loop = asyncio.get_running_loop()

    async with _sheets_semaphore:
        for attempt in range(retries):
            try:
                return await loop.run_in_executor(None, fn)

            except gspread.exceptions.APIError as exc:
                status = getattr(exc.response, "status_code", 0)
                if status == 429 or "quota" in str(exc).lower():
                    print(f"[Sheets] Quota hit (attempt {attempt+1}). Waiting {delay:.1f}s…")
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                raise

            except Exception as exc:
                print(f"[Sheets] Error (attempt {attempt+1}): {exc}")
                if attempt < retries - 1:
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    raise

    raise RuntimeError("safe_sheets_call: all retries exhausted")


# -------------------------------------------------
#  CONFIG
# -------------------------------------------------
SPREADSHEET_ID   = '1fUkh8LhRhRqQq9MjlgzM4bI2sIhbkvmTrMYFehqNJMs'
DISCORD_TOKEN    = os.getenv('DISCORD_TOKEN')

APPS_SCRIPT_URL = 'https://script.google.com/macros/s/AKfycbwiKn7Xo_nGyfRvtH3z8LEPYPbxXOKjvM8DRCfbg2gbYO5jSfUEv9ZT5unVUaJoVIk/exec'

SELF_PING_INTERVAL   = 300
SELF_PING_CHANNEL_ID = os.getenv('SELF_PING_CHANNEL_ID')

ACADEMY_TIMETABLE_CHANNEL_ID      = 0
SIXTH_FORM_TIMETABLE_CHANNEL_ID   = 0
ACADEMY_STAFF_ROLE_ID             = 1438203308178145310
SIXTH_FORM_STAFF_ROLE_ID          = 1459540584414052373
TIMETABLE_POST_TIME               = "21:15"
YEAR_LEADER_ROLE_ID               = 0
TIMETABLE_ADMIN_ROLE_ID           = 1438202314476228678

ROBLOX_COOKIE   = os.getenv('ROBLOX_AUTH_TOKEN')
ROBLOX_GROUP_ID = 779411059

ROLE_NAME_MAP = {
    "School Staff":                  "Teaching Staff",
    "Deputy Head of Year 7":         "Deputy Head of Year",
    "Deputy Head of Year 8":         "Deputy Head of Year",
    "Deputy Head of Year 9":         "Deputy Head of Year",
    "Deputy Head of Year 10":        "Deputy Head of Year",
    "Deputy Head of Year 11":        "Deputy Head of Year",
    "Deputy Head of Sixth Form":     "Deputy Head of Year",
    "Head of Year 7":                "Head of Year",
    "Head of Year 8":                "Head of Year",
    "Head of Year 9":                "Head of Year",
    "Head of Year 10":               "Head of Year",
    "Head of Year 11":               "Head of Year",
    "Head of Sixth Form":            "Head of Year",
    "Deputy Head of Lower Level":    "Deputy Head of Level",
    "Deputy Head of Middle Level":   "Deputy Head of Level",
    "Deputy Head of Upper Level":    "Deputy Head of Level",
    "Head of Lower Level":           "Head of Level",
    "Head of Middle Level":          "Head of Level",
    "Head of Upper Level":           "Head of Level",
    "Site Staff":                    "Site Staff",
    "Site Manager":                  "Site Manager",
    "Assistant Headteacher":         "Assistant Headteacher",
    "Deputy Headteacher":            "Deputy Headteacher",
    "Headteacher":                   "Headteacher",
    "Executive Headteacher":         "Executive Headteacher",
    "Chief Education Officer":       "Chief Education Officer",
}

INFINITE_ROLES = {
    "School Staff",
}

ALL_STAFF_SHEET          = "All Staff"
CURRENT_STAFF_SHEET      = "Current Staff"
ATTENDANCE_SHEET         = "Attendance Register"
ROLES_LIST_SHEET         = "Roles List"
EDIT_STAFF_SHEET         = "Edit Staff"
REMOVE_STAFF_LOG_SHEET   = "Remove Staff Log"
BLACKLISTED_STAFF_SHEET  = "Blacklisted Staff"

ALL_STAFF_DATA_START = 5
ALL_STAFF_NAME_COL   = 3

CURRENT_STAFF_DATA_START     = 5
CURRENT_STAFF_NAME_COL       = 3
CURRENT_STAFF_ATTENDANCE_COL = 9

ATTEND_DATA_START  = 5
ATTEND_NAME_COL    = 1
ATTEND_CHECK_START = 2
ATTEND_CHECK_END   = 9

# ------------------------------------------------------------------
#  Remove Staff Log column layout (1-indexed columns, 0-indexed below)
#  Col B (index 1) = Teaching Name
#  Col C (index 2) = Date of Removal
#  Col D (index 3) = Discord ID
#  Col E (index 4) = Reason for Removal
# ------------------------------------------------------------------
RSL_NAME_COL       = 1   # Col B - Teaching Name
RSL_DATE_COL       = 2   # Col C - Date of Removal
RSL_REASON_COL     = 3   # Col D - Reason
RSL_DISCORD_ID_COL = 4   # Col E - Discord ID
RSL_DATA_START     = 5   # First data row
# ------------------------------------------------------------------
#  Blacklisted Staff column layout
#  Col B (index 1) = Discord ID
#  Col C (index 2) = Roblox Username
#  Col D (index 3) = Date of Blacklist
#  Col E (index 4) = Reason
# ------------------------------------------------------------------
BL_DISCORD_ID_COL = 1   # Col B
BL_ROBLOX_COL     = 2   # Col C
BL_DATE_COL       = 3   # Col D
BL_REASON_COL     = 4   # Col E
BL_DATA_START     = 5   # First data row

FIELD_MAP = {
    "role":            ("Role",             "C6"),
    "roblox_username": ("Roblox Username",  "D6"),
    "teaching_name":   ("Teaching Name",    "E6"),
    "area":            ("Area",             "F6"),
    "discord_id":      ("Discord User ID",  "I6"),
}

BLOCKED_APP_LOG_CHANNEL_ID = 1495061332741853438
COMMAND_LOG_CHANNEL_ID     = 1495796902082646209

# -------------------------------------------------
#  NAME INITIALING LOGIC
# -------------------------------------------------

INITIALLED_ROBLOX_RANKS = {
    "Teaching Staff",
    "Deputy Head of Year",
    "Head of Year",
    "Deputy Head of Level",
    "Head of Level",
}

INITIALLED_SHEET_ROLES = {
    sheet_role
    for sheet_role, roblox_rank in ROLE_NAME_MAP.items()
    if roblox_rank in INITIALLED_ROBLOX_RANKS
}

KNOWN_TITLES = {
    "mr", "mrs", "miss", "ms", "mx", "dr", "prof", "professor",
    "sir", "rev", "reverend", "fr", "father", "lord", "lady",
}


def initial_middle_names(full_name: str) -> str:
    parts = full_name.strip().split()

    if len(parts) <= 1:
        return full_name

    has_title = parts[0].lower().rstrip(".") in KNOWN_TITLES

    if has_title:
        if len(parts) == 2:
            return full_name
        title         = parts[0]
        surname       = parts[-1]
        first_initial = parts[1][0].upper() + "."
        return f"{title} {first_initial} {surname}"
    else:
        if len(parts) == 2:
            first_initial = parts[0][0].upper() + "."
            return f"{first_initial} {parts[1]}"
        first_initial = parts[0][0].upper() + "."
        surname       = parts[-1]
        return f"{first_initial} {surname}"


def format_display_name(teaching_name: str, sheet_role: str) -> str:
    if sheet_role in INITIALLED_SHEET_ROLES:
        return initial_middle_names(teaching_name)
    return teaching_name


# -------------------------------------------------
#  DISCORD ROLE & NICKNAME CONFIG
# -------------------------------------------------

DISCORD_RANK_ROLE_IDS: dict[str, int] = {
    "Headteacher":           1484861142907097108,
    "Deputy Headteacher":    1484861834191437858,
    "Assistant Headteacher": 1484861922313506939,
    "Site Staff":            1484862019873148998,
    "Head of Level":         1484862221644206230,
    "Deputy Head of Level":  1484862355090309152,
    "Head of Year":          1484862436061347983,
    "Deputy Head of Year":   1484862545063051340,
    "Teaching Staff":        1484862783626543185,
}

DISCORD_EXTRA_ROLE_IDS: dict[str, int] = {
    "Senior Leadership Team": 1484862178698727557,
    "Year Leadership Team":   1484862740945174569,
    "Staff":                  1484863012933210143,
    "Sixth Form Leadership":  1485053144952995911,
    "Year 11 Leadership":     1485053638400540742,
    "Year 10 Leadership":     1485053691500429372,
    "Year 9 Leadership":      1485053716859060436,
    "Year 8 Leadership":      1485053745531195432,
    "Year 7 Leadership":      1485053768939864254,
}

ALL_MANAGED_ROLE_IDS: set[int] = (
    set(DISCORD_RANK_ROLE_IDS.values()) | set(DISCORD_EXTRA_ROLE_IDS.values())
)


def get_discord_roles_for_sheet_role(sheet_role: str) -> list[int]:
    R = DISCORD_RANK_ROLE_IDS
    E = DISCORD_EXTRA_ROLE_IDS
    staff   = E["Staff"]
    slt     = E["Senior Leadership Team"]
    ylt     = E["Year Leadership Team"]
    sf_lead = E["Sixth Form Leadership"]
    hol     = R["Head of Level"]
    dhol    = R["Deputy Head of Level"]
    hoy     = R["Head of Year"]
    dhoy    = R["Deputy Head of Year"]
    ts      = R["Teaching Staff"]
    site    = R["Site Staff"]
    aht     = R["Assistant Headteacher"]
    dht     = R["Deputy Headteacher"]
    ht      = R["Headteacher"]

    mapping: dict[str, list[int]] = {
        "Headteacher":                 [staff, slt, ht],
        "Deputy Headteacher":          [staff, slt, dht],
        "Assistant Headteacher":       [staff, slt, aht],
        "Head of Upper Level":         [staff, ylt, sf_lead, hol],
        "Deputy Head of Upper Level":  [staff, ylt, sf_lead, dhol],
        "Head of Middle Level":        [staff, ylt, E["Year 11 Leadership"], E["Year 10 Leadership"], hol],
        "Deputy Head of Middle Level": [staff, ylt, E["Year 11 Leadership"], E["Year 10 Leadership"], dhol],
        "Head of Lower Level":         [staff, ylt, E["Year 9 Leadership"], E["Year 8 Leadership"], E["Year 7 Leadership"], hol],
        "Deputy Head of Lower Level":  [staff, ylt, E["Year 9 Leadership"], E["Year 8 Leadership"], E["Year 7 Leadership"], dhol],
        "Head of Sixth Form":          [staff, ylt, sf_lead, hoy],
        "Deputy Head of Sixth Form":   [staff, ylt, sf_lead, dhoy],
        "Head of Year 11":             [staff, ylt, E["Year 11 Leadership"], hoy],
        "Deputy Head of Year 11":      [staff, ylt, E["Year 11 Leadership"], dhoy],
        "Head of Year 10":             [staff, ylt, E["Year 10 Leadership"], hoy],
        "Deputy Head of Year 10":      [staff, ylt, E["Year 10 Leadership"], dhoy],
        "Head of Year 9":              [staff, ylt, E["Year 9 Leadership"], hoy],
        "Deputy Head of Year 9":       [staff, ylt, E["Year 9 Leadership"], dhoy],
        "Head of Year 8":              [staff, ylt, E["Year 8 Leadership"], hoy],
        "Deputy Head of Year 8":       [staff, ylt, E["Year 8 Leadership"], dhoy],
        "Head of Year 7":              [staff, ylt, E["Year 7 Leadership"], hoy],
        "Deputy Head of Year 7":       [staff, ylt, E["Year 7 Leadership"], dhoy],
        "School Staff":                [staff, ts],
        "Site Staff":                  [staff, site],
        "Site Manager":                [staff, site],
    }
    return mapping.get(sheet_role, [staff])


def get_nickname_for_sheet_role(teaching_name: str, sheet_role: str) -> str:
    display_name = format_display_name(teaching_name, sheet_role)

    suffix_map: dict[str, str] = {
        "Headteacher":                 "[SLT]",
        "Deputy Headteacher":          "[SLT]",
        "Assistant Headteacher":       "[SLT]",
        "Head of Upper Level":         "| HOUL",
        "Deputy Head of Upper Level":  "| DHOUL",
        "Head of Middle Level":        "| HOML",
        "Deputy Head of Middle Level": "| DHOML",
        "Head of Lower Level":         "| HOLL",
        "Deputy Head of Lower Level":  "| DHOLL",
        "Head of Sixth Form":          "| HOSF",
        "Deputy Head of Sixth Form":   "| DHOSF",
        "Head of Year 7":              "| HOY7",
        "Deputy Head of Year 7":       "| DHOY7",
        "Head of Year 8":              "| HOY8",
        "Deputy Head of Year 8":       "| DHOY8",
        "Head of Year 9":              "| HOY9",
        "Deputy Head of Year 9":       "| DHOY9",
        "Head of Year 10":             "| HOY10",
        "Deputy Head of Year 10":      "| DHOY10",
        "Head of Year 11":             "| HOY11",
        "Deputy Head of Year 11":      "| DHOY11",
    }

    suffix = suffix_map.get(sheet_role)
    if suffix is None:
        return display_name

    return f"{display_name} {suffix}"


async def apply_discord_roles_and_nick(
    guild: discord.Guild,
    member: discord.Member,
    sheet_role: str,
    teaching_name: str,
    *,
    embed: discord.Embed | None = None,
) -> None:
    desired_ids = get_discord_roles_for_sheet_role(sheet_role)
    desired_role_objects = [
        guild.get_role(rid) for rid in desired_ids
        if guild.get_role(rid) is not None
    ]

    roles_to_remove = [r for r in member.roles if r.id in ALL_MANAGED_ROLE_IDS]

    try:
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="Staff role update")
        if desired_role_objects:
            await member.add_roles(*desired_role_objects, reason=f"Hired/edited as {sheet_role}")
        role_names = ", ".join(r.name for r in desired_role_objects) or "None"
        if embed:
            embed.add_field(name="Discord Roles", value=f"✅ Set: {role_names}", inline=False)
    except discord.Forbidden:
        if embed:
            embed.add_field(name="Discord Roles", value="⚠️ Missing permissions to manage roles", inline=False)
    except Exception as e:
        if embed:
            embed.add_field(name="Discord Roles", value=f"⚠️ Error: {e}", inline=False)

    new_nick = get_nickname_for_sheet_role(teaching_name, sheet_role)
    new_nick = new_nick[:32]
    try:
        await member.edit(nick=new_nick, reason=f"Staff role: {sheet_role}")
        if embed:
            embed.add_field(name="Nickname", value=f"✅ Set to: `{new_nick}`", inline=False)
    except discord.Forbidden:
        if embed:
            embed.add_field(name="Nickname", value="⚠️ Missing permissions to change nickname", inline=False)
    except Exception as e:
        if embed:
            embed.add_field(name="Nickname", value=f"⚠️ Error: {e}", inline=False)


async def clear_discord_roles_and_reset_nick(
    guild: discord.Guild,
    member: discord.Member,
    roblox_username: str,
    *,
    embed: discord.Embed | None = None,
) -> None:
    roles_to_remove = [r for r in member.roles if r.id in ALL_MANAGED_ROLE_IDS]
    try:
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="Staff removed")
        if embed:
            embed.add_field(name="Discord Roles", value="✅ All staff roles removed", inline=False)
    except discord.Forbidden:
        if embed:
            embed.add_field(name="Discord Roles", value="⚠️ Missing permissions to manage roles", inline=False)
    except Exception as e:
        if embed:
            embed.add_field(name="Discord Roles", value=f"⚠️ Error: {e}", inline=False)

    nick = roblox_username[:32] if roblox_username else None
    try:
        await member.edit(nick=nick, reason="Staff removed")
        if embed:
            embed.add_field(name="Nickname", value=f"✅ Reset to: `{nick or 'cleared'}`", inline=False)
    except discord.Forbidden:
        if embed:
            embed.add_field(name="Nickname", value="⚠️ Missing permissions to change nickname", inline=False)
    except Exception as e:
        if embed:
            embed.add_field(name="Nickname", value=f"⚠️ Error: {e}", inline=False)


async def get_discord_member_by_id(guild: discord.Guild, discord_id: str) -> discord.Member | None:
    try:
        uid    = int(discord_id)
        member = guild.get_member(uid)
        if member is None:
            member = await guild.fetch_member(uid)
        return member
    except Exception:
        return None


async def get_discord_id_for_staff(teaching_name: str) -> str | None:
    try:
        all_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(CURRENT_STAFF_SHEET).get_all_values()
        )
        for row in all_data[CURRENT_STAFF_DATA_START - 1:]:
            if len(row) > CURRENT_STAFF_NAME_COL and row[CURRENT_STAFF_NAME_COL].strip().lower() == teaching_name.lower():
                discord_id = safe_get(row, 7)
                return discord_id if discord_id != "N/A" else None
    except Exception as e:
        print(f"[Discord] Discord ID lookup error: {e}")
    return None


async def get_role_for_staff(teaching_name: str) -> str | None:
    try:
        all_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(CURRENT_STAFF_SHEET).get_all_values()
        )
        for row in all_data[CURRENT_STAFF_DATA_START - 1:]:
            if len(row) > CURRENT_STAFF_NAME_COL and row[CURRENT_STAFF_NAME_COL].strip().lower() == teaching_name.lower():
                role = safe_get(row, 1)
                return role if role != "N/A" else None
    except Exception as e:
        print(f"[Discord] Role lookup error: {e}")
    return None


print("Starting bot...", flush=True)
print(f"Token present: {bool(DISCORD_TOKEN)}", flush=True)
print(f"Google credentials present: {bool(os.getenv('GOOGLE_CREDENTIALS'))}", flush=True)
print(f"Roblox cookie present: {bool(ROBLOX_COOKIE)}", flush=True)


# -------------------------------------------------
#  APPLICATION ELIGIBILITY HELPERS
# -------------------------------------------------

async def check_application_eligibility(discord_id: str) -> tuple[bool, str]:
    """
    Returns (can_apply, reason_code).
    reason_code: "blacklisted" | "recent_staff" | ""
    """
    # 1. Check blacklist — Discord ID in col B (index 1) of Blacklisted Staff
    try:
        bl_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(BLACKLISTED_STAFF_SHEET).get_all_values()
        )
        for row in bl_data[BL_DATA_START - 1:]:
            if safe_get(row, BL_DISCORD_ID_COL) == discord_id:
                return False, "blacklisted"
    except gspread.WorksheetNotFound:
        print(f"[Apply] '{BLACKLISTED_STAFF_SHEET}' sheet not found — skipping blacklist check")
    except Exception as e:
        print(f"[Apply] Blacklist check error: {e}")

    # 2. Check recent removal — Discord ID in col D (index 3), date in col C (index 2)
    try:
        rsl_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(REMOVE_STAFF_LOG_SHEET).get_all_values()
        )
        two_weeks_ago = datetime.now() - timedelta(weeks=2)

        for row in rsl_data[RSL_DATA_START - 1:]:
            row_discord_id = safe_get(row, RSL_DISCORD_ID_COL)
            if row_discord_id != discord_id:
                continue

            date_str = safe_get(row, RSL_DATE_COL)
            if not date_str or date_str == "N/A":
                continue

            removal_date = None
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%B %d, %Y", "%d %B %Y", "%Y/%m/%d"):
                try:
                    removal_date = datetime.strptime(date_str, fmt)
                    break
                except ValueError:
                    continue

            if removal_date is None:
                print(f"[Apply] Could not parse removal date '{date_str}' for Discord ID {discord_id} — skipping row")
                continue

            if removal_date > two_weeks_ago:
                return False, "recent_staff"

    except gspread.WorksheetNotFound:
        print(f"[Apply] '{REMOVE_STAFF_LOG_SHEET}' sheet not found — skipping recent-staff check")
    except Exception as e:
        print(f"[Apply] Recent staff check error: {e}")

    return True, ""


# -------------------------------------------------
#  ROBLOX API CLASS
# -------------------------------------------------
class RobloxAPI:
    def __init__(self, security_cookie: str, group_id: int):
        self.security_cookie = security_cookie.strip()
        self.group_id        = group_id
        self.session         = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

    def get_user_id_by_username(self, username: str) -> int | None:
        try:
            resp = self.session.post(
                "https://users.roblox.com/v1/usernames/users",
                json={"usernames": [username], "excludeBannedUsers": False},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            return data[0]["id"] if data else None
        except Exception as e:
            print(f"[Roblox] Username → ID error: {e}")
            return None

    def get_group_roles(self) -> list[dict]:
        try:
            resp = self.session.get(
                f"https://groups.roblox.com/v1/groups/{self.group_id}/roles",
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("roles", [])
        except Exception as e:
            print(f"[Roblox] Get group roles error: {e}")
            return []

    def find_role_id_by_name(self, roblox_rank_name: str) -> int | None:
        for role in self.get_group_roles():
            if role["name"].lower() == roblox_rank_name.lower():
                return role["id"]
        return None

    def find_rank_1_role_id(self) -> int | None:
        roles = self.get_group_roles()
        if not roles:
            return None
        rank_1 = next((r for r in roles if r["rank"] == 6), None)
        if rank_1:
            return rank_1["id"]
        return sorted(roles, key=lambda r: r["rank"])[0]["id"]

    def change_user_rank(self, user_id: int, role_id: int) -> tuple[bool, str]:
        url     = f"https://groups.roblox.com/v1/groups/{self.group_id}/users/{user_id}"
        headers = {
            "Cookie":       f".ROBLOSECURITY={self.security_cookie}",
            "Content-Type": "application/json",
            "Accept":       "application/json",
        }
        payload = {"roleId": role_id}

        try:
            resp = self.session.patch(url, headers=headers, json=payload, timeout=10)

            if resp.status_code == 403 and "X-CSRF-TOKEN" in resp.headers:
                headers["X-CSRF-TOKEN"] = resp.headers["X-CSRF-TOKEN"]
                resp = self.session.patch(url, headers=headers, json=payload, timeout=10)

            if resp.status_code == 200:
                return True, ""
            if resp.status_code == 401:
                return False, "Authentication failed — cookie may be expired."
            if resp.status_code == 403:
                return False, "Insufficient permissions to rank this user."
            if resp.status_code == 400:
                return False, "Bad request — invalid user ID or role ID."
            if resp.status_code == 404:
                return False, "User not found in the group."

            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"

        except Exception as e:
            print(f"[Roblox] change_user_rank error: {e}")
            return False, f"Network error: {e}"


def get_roblox_api() -> RobloxAPI | None:
    if not ROBLOX_COOKIE:
        return None
    return RobloxAPI(ROBLOX_COOKIE, ROBLOX_GROUP_ID)


async def roblox_get_user_id(username: str) -> int | None:
    api = get_roblox_api()
    if not api:
        return None
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, api.get_user_id_by_username, username)


async def roblox_set_rank_by_sheet_role(
    roblox_username: str,
    sheet_role_name: str,
) -> tuple[bool, str]:
    api = get_roblox_api()
    if not api:
        return False, "ROBLOX_AUTH_TOKEN environment variable is not set."

    roblox_rank_name = ROLE_NAME_MAP.get(sheet_role_name)
    if not roblox_rank_name:
        return False, f"No Roblox rank mapped for sheet role **{sheet_role_name}**."

    loop = asyncio.get_running_loop()

    user_id = await loop.run_in_executor(None, api.get_user_id_by_username, roblox_username)
    if not user_id:
        return False, f"Roblox user **{roblox_username}** not found."

    role_id = await loop.run_in_executor(None, api.find_role_id_by_name, roblox_rank_name)
    if not role_id:
        return False, f"Rank **{roblox_rank_name}** not found in the group."

    success, err = await loop.run_in_executor(None, api.change_user_rank, user_id, role_id)
    if success:
        return True, roblox_rank_name
    return False, err


async def roblox_demote_to_rank_1(roblox_username: str) -> tuple[bool, str]:
    api = get_roblox_api()
    if not api:
        return False, "ROBLOX_AUTH_TOKEN environment variable is not set."

    loop = asyncio.get_running_loop()

    user_id = await loop.run_in_executor(None, api.get_user_id_by_username, roblox_username)
    if not user_id:
        return False, f"Roblox user **{roblox_username}** not found."

    role_id = await loop.run_in_executor(None, api.find_rank_1_role_id)
    if not role_id:
        return False, "Could not find rank 1 role in the group."

    success, err = await loop.run_in_executor(None, api.change_user_rank, user_id, role_id)
    return success, err


# -------------------------------------------------
#  BOT SETUP
# -------------------------------------------------
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

sheets_client = None
spreadsheet   = None

last_ping_time    = None
last_ping_latency = None
ping_failures     = 0

staff_names_cache      = []
staff_names_cache_time = None
CACHE_DURATION         = 300

academy_current_message_id    = None
sixth_form_current_message_id = None

academy_timetable = {
    "Year 7":                {"Form": "", "Period 1": "", "Period 2": "", "Period 3": "", "Period 4": "", "After School": ""},
    "Year 8":                {"Form": "", "Period 1": "", "Period 2": "", "Period 3": "", "Period 4": "", "After School": ""},
    "Year 9":                {"Form": "", "Period 1": "", "Period 2": "", "Period 3": "", "Period 4": "", "After School": ""},
    "Year 10":               {"Form": "", "Period 1": "", "Period 2": "", "Period 3": "", "Period 4": "", "After School": ""},
    "Year 11":               {"Form": "", "Period 1": "", "Period 2": "", "Period 3": "", "Period 4": "", "After School": ""},
    "Additional Needs Unit": {"Form": "", "Period 1": "", "Period 2": "", "Period 3": "", "Period 4": "", "After School": ""},
    "Isolation":             {"Period 1": "", "Period 2": "", "Period 3": "", "Period 4": ""},
    "Detention":             {"Lunch time": "", "After-School Club": ""},
}

sixth_form_timetable = {
    "Year 12":               {"Form": "", "Period 1": "", "Period 2": "", "Period 3": "", "Period 4": "", "After School": ""},
    "Year 13":               {"Form": "", "Period 1": "", "Period 2": "", "Period 3": "", "Period 4": "", "After School": ""},
    "Additional Needs Unit": {"Form": "", "Period 1": "", "Period 2": "", "Period 3": "", "Period 4": "", "After School": ""},
    "Isolation":             {"Period 1": "", "Period 2": "", "Period 3": "", "Period 4": ""},
    "Detention":             {"Lunch time": "", "After-School Club": ""},
}


def setup_google_sheets():
    global sheets_client, spreadsheet
    try:
        credentials_json = os.getenv("GOOGLE_CREDENTIALS")
        if credentials_json:
            creds_dict = json.loads(credentials_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        else:
            print("GOOGLE_CREDENTIALS env var not set, falling back to credentials.json file")
            creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)

        sheets_client = gspread.authorize(creds)
        spreadsheet   = sheets_client.open_by_key(SPREADSHEET_ID)
        print("Connected to Google Sheets!")
        return True
    except Exception as e:
        print(f"Google Sheets connection failed: {e}")
        return False


# -------------------------------------------------
#  TIMETABLE HELPERS
# -------------------------------------------------
def format_timetable_message(timetable_data):
    message = ""
    for year, periods in timetable_data.items():
        message += f"**{year}**\n"
        for period, booking in periods.items():
            message += f"{period}: {booking}\n" if booking else f"{period}: \n"
        message += "\n"
    if "Detention" in timetable_data:
        message = message.replace("**Detention**\n", "**Detention**\n*Only AHOY+ can book*\n")
    return message


def is_timetable_full(timetable_data):
    return all(booking for periods in timetable_data.values() for booking in periods.values())


def get_available_periods(timetable_data, year):
    if year not in timetable_data:
        return []
    return [period for period, booking in timetable_data[year].items() if not booking]


async def get_staff_teaching_name(discord_id: str):
    try:
        all_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(CURRENT_STAFF_SHEET).get_all_values()
        )
        for row in all_data[CURRENT_STAFF_DATA_START - 1:]:
            if len(row) > 7 and row[7].strip() == discord_id:
                return row[CURRENT_STAFF_NAME_COL].strip()
    except Exception as e:
        print(f"Error getting teaching name: {e}")
    return None


async def get_roblox_username_for_staff(teaching_name: str) -> str | None:
    try:
        all_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(CURRENT_STAFF_SHEET).get_all_values()
        )
        for row in all_data[CURRENT_STAFF_DATA_START - 1:]:
            if len(row) > CURRENT_STAFF_NAME_COL and row[CURRENT_STAFF_NAME_COL].strip().lower() == teaching_name.lower():
                username = safe_get(row, 2)
                return username if username != "N/A" else None
    except Exception as e:
        print(f"[Roblox] Sheet username lookup error: {e}")
    return None


def has_year_leader_role(interaction: discord.Interaction) -> bool:
    if YEAR_LEADER_ROLE_ID == 0:
        return True
    return any(role.id == YEAR_LEADER_ROLE_ID for role in interaction.user.roles)


def has_timetable_admin_role(interaction: discord.Interaction) -> bool:
    return any(role.id == TIMETABLE_ADMIN_ROLE_ID for role in interaction.user.roles)


# -------------------------------------------------
#  TIMETABLE TASKS
# -------------------------------------------------
def _fresh_academy_timetable():
    return {
        "Year 7":                {"Form": "", "Period 1": "", "Period 2": "", "Period 3": "", "Period 4": "", "After School": ""},
        "Year 8":                {"Form": "", "Period 1": "", "Period 2": "", "Period 3": "", "Period 4": "", "After School": ""},
        "Year 9":                {"Form": "", "Period 1": "", "Period 2": "", "Period 3": "", "Period 4": "", "After School": ""},
        "Year 10":               {"Form": "", "Period 1": "", "Period 2": "", "Period 3": "", "Period 4": "", "After School": ""},
        "Year 11":               {"Form": "", "Period 1": "", "Period 2": "", "Period 3": "", "Period 4": "", "After School": ""},
        "Additional Needs Unit": {"Form": "", "Period 1": "", "Period 2": "", "Period 3": "", "Period 4": "", "After School": ""},
        "Isolation":             {"Period 1": "", "Period 2": "", "Period 3": "", "Period 4": ""},
        "Detention":             {"Lunch time": "", "After-School Club": ""},
    }

def _fresh_sixth_form_timetable():
    return {
        "Year 12":               {"Form": "", "Period 1": "", "Period 2": "", "Period 3": "", "Period 4": "", "After School": ""},
        "Year 13":               {"Form": "", "Period 1": "", "Period 2": "", "Period 3": "", "Period 4": "", "After School": ""},
        "Additional Needs Unit": {"Form": "", "Period 1": "", "Period 2": "", "Period 3": "", "Period 4": "", "After School": ""},
        "Isolation":             {"Period 1": "", "Period 2": "", "Period 3": "", "Period 4": ""},
        "Detention":             {"Lunch time": "", "After-School Club": ""},
    }


@tasks.loop(minutes=1)
async def timetable_post_task():
    global academy_current_message_id, sixth_form_current_message_id
    global academy_timetable, sixth_form_timetable

    now = datetime.now()
    if now.strftime("%H:%M") != TIMETABLE_POST_TIME:
        return

    print(f"[Timetable] Posting daily timetables at {TIMETABLE_POST_TIME}")
    academy_timetable    = _fresh_academy_timetable()
    sixth_form_timetable = _fresh_sixth_form_timetable()

    try:
        ch = bot.get_channel(ACADEMY_TIMETABLE_CHANNEL_ID)
        if ch:
            msg = await ch.send(f"📅 **Academy Timetable - {now.strftime('%B %d, %Y')}**\n\n{format_timetable_message(academy_timetable)}")
            academy_current_message_id = msg.id
            await ch.send(f"<@&{ACADEMY_STAFF_ROLE_ID}> Let's get everything booked!")
    except Exception as e:
        print(f"[Timetable] Error posting Academy timetable: {e}")

    try:
        ch = bot.get_channel(SIXTH_FORM_TIMETABLE_CHANNEL_ID)
        if ch:
            msg = await ch.send(f"📅 **Sixth Form Timetable - {now.strftime('%B %d, %Y')}**\n\n{format_timetable_message(sixth_form_timetable)}")
            sixth_form_current_message_id = msg.id
            await ch.send(f"<@&{SIXTH_FORM_STAFF_ROLE_ID}> Let's get everything booked!")
    except Exception as e:
        print(f"[Timetable] Error posting Sixth Form timetable: {e}")


@tasks.loop(minutes=1)
async def timetable_reminder_task():
    now          = datetime.now()
    current_time = now.strftime("%H:%M")
    reminder_times = ["21:15", "23:00", "07:30", "15:00", "16:30", "18:00", "19:00"]

    if current_time not in reminder_times:
        return

    minute_key = now.strftime("%Y-%m-%d-%H-%M")
    if not hasattr(timetable_reminder_task, 'sent_reminders'):
        timetable_reminder_task.sent_reminders = set()
    if minute_key in timetable_reminder_task.sent_reminders:
        return

    timetable_reminder_task.sent_reminders.add(minute_key)
    cutoff = (now - timedelta(days=1)).strftime("%Y-%m-%d-%H-%M")
    timetable_reminder_task.sent_reminders = {k for k in timetable_reminder_task.sent_reminders if k > cutoff}

    print(f"[Timetable] Sending scheduled reminders at {current_time}")

    if academy_current_message_id and not is_timetable_full(academy_timetable):
        try:
            ch = bot.get_channel(ACADEMY_TIMETABLE_CHANNEL_ID)
            if ch:
                await ch.send(f"<@&{ACADEMY_STAFF_ROLE_ID}> Let's get everything booked!")
        except Exception as e:
            print(f"[Timetable] Academy reminder error: {e}")

    if sixth_form_current_message_id and not is_timetable_full(sixth_form_timetable):
        try:
            ch = bot.get_channel(SIXTH_FORM_TIMETABLE_CHANNEL_ID)
            if ch:
                await ch.send(f"<@&{SIXTH_FORM_STAFF_ROLE_ID}> Let's get everything booked!")
        except Exception as e:
            print(f"[Timetable] Sixth Form reminder error: {e}")


@timetable_post_task.before_loop
async def before_timetable_post():
    await bot.wait_until_ready()
    print("Timetable posting task started")

@timetable_reminder_task.before_loop
async def before_timetable_reminder():
    await bot.wait_until_ready()
    print("Timetable reminder task started")


# -------------------------------------------------
#  SELF-PING TASK
# -------------------------------------------------
@tasks.loop(seconds=SELF_PING_INTERVAL)
async def self_ping_task():
    global last_ping_time, last_ping_latency, ping_failures
    try:
        ping_start = datetime.now()
        latency_ms = round(bot.latency * 1000)
        if bot.is_ready():
            last_ping_time    = ping_start
            last_ping_latency = latency_ms
            ping_failures     = 0
            print(f"[Self-Ping] {ping_start.strftime('%Y-%m-%d %H:%M:%S')} | Latency: {latency_ms}ms | Status: Online")
            if latency_ms > 1000 and SELF_PING_CHANNEL_ID:
                ch = bot.get_channel(int(SELF_PING_CHANNEL_ID))
                if ch:
                    await ch.send(f"⚠️ High latency detected: {latency_ms}ms")
        else:
            ping_failures += 1
            print(f"[Self-Ping] Bot not ready | Failures: {ping_failures}")
            if ping_failures >= 3 and SELF_PING_CHANNEL_ID:
                ch = bot.get_channel(int(SELF_PING_CHANNEL_ID))
                if ch:
                    await ch.send(f"❌ Bot has failed {ping_failures} consecutive health checks!")
    except Exception as e:
        ping_failures += 1
        print(f"[Self-Ping] Error: {e}")

@self_ping_task.before_loop
async def before_self_ping():
    await bot.wait_until_ready()
    print("Self-ping task started")


@bot.event
async def on_ready():
    global last_ping_time, last_ping_latency
    print(f'{bot.user} connected to Discord!')
    print(f'In {len(bot.guilds)} server(s)')
    setup_google_sheets()

    if not hasattr(bot, '_cache_loaded'):
        bot._cache_loaded = True
        asyncio.get_event_loop().run_in_executor(None, refresh_staff_names_cache)

    last_ping_time    = datetime.now()
    last_ping_latency = round(bot.latency * 1000)

    await bot.change_presence(activity=discord.CustomActivity(name="Winstree Academy's Assistant. Run /hello to try me out!"))

    if not hasattr(bot, '_synced'):
        bot._synced = True
        try:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} slash command(s)")
        except Exception as e:
            print(f"Failed to sync commands: {e}")
    else:
        print("Reconnected — skipping command sync (already synced this session)")

    if not self_ping_task.is_running():
        self_ping_task.start()
    if not timetable_post_task.is_running():
        timetable_post_task.start()
    if not timetable_reminder_task.is_running():
        timetable_reminder_task.start()
    if not weekly_tiktok_task.is_running():
        weekly_tiktok_task.start()


# -------------------------------------------------
#  HELPERS
# -------------------------------------------------
def is_checked(cell_value: str) -> bool:
    return str(cell_value).strip().upper() in {'TRUE', 'YES', 'Y', '1', 'X', '\u2713', '\u2714'}


def safe_get(row: list, index: int, default: str = "N/A") -> str:
    if index < len(row) and str(row[index]).strip():
        return str(row[index]).strip()
    return default


def refresh_staff_names_cache():
    global staff_names_cache, staff_names_cache_time
    try:
        if not spreadsheet:
            return
        worksheet = spreadsheet.worksheet(CURRENT_STAFF_SHEET)
        all_data  = worksheet.get_all_values()
        data_rows = all_data[CURRENT_STAFF_DATA_START - 1:]
        staff_names_cache = [
            row[CURRENT_STAFF_NAME_COL].strip()
            for row in data_rows
            if len(row) > CURRENT_STAFF_NAME_COL and row[CURRENT_STAFF_NAME_COL].strip()
        ]
        staff_names_cache_time = datetime.now()
        print(f"Staff names cache refreshed: {len(staff_names_cache)} names loaded")
    except Exception as e:
        print(f"Failed to refresh staff names cache: {e}")


# -------------------------------------------------
#  AUTOCOMPLETE
# -------------------------------------------------
async def staff_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    try:
        global staff_names_cache, staff_names_cache_time
        if not staff_names_cache_time or (datetime.now() - staff_names_cache_time).total_seconds() > CACHE_DURATION:
            refresh_staff_names_cache()
        filtered = [n for n in staff_names_cache if current.lower() in n.lower()]
        return [app_commands.Choice(name=n, value=n) for n in filtered[:25]]
    except Exception as e:
        print(f"Autocomplete error: {e}")
        return []

async def edit_staff_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    return await staff_autocomplete(interaction, current)

async def position_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    try:
        all_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(ROLES_LIST_SHEET).get_all_values()
        )
        roles = []
        for row in all_data:
            for cell in row:
                cell = cell.strip()
                if cell and cell not in roles:
                    roles.append(cell)
        filtered = [r for r in roles if current.lower() in r.lower()]
        return [app_commands.Choice(name=r, value=r) for r in filtered[:25]]
    except Exception as e:
        print(f"Position autocomplete error: {e}")
        return []

async def edit_value_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    field = None
    try:
        for opt in interaction.data.get('options', []):
            if opt.get('name') == 'field':
                field = opt.get('value')
                break
    except Exception:
        pass
    if field == 'role':
        return await position_autocomplete(interaction, current)
    elif field == 'area':
        choices  = ["Academy", "Sixth Form"]
        filtered = [c for c in choices if current.lower() in c.lower()]
        return [app_commands.Choice(name=c, value=c) for c in filtered]
    return []


# -------------------------------------------------
#  BLACKLISTED STAFF AUTOCOMPLETE
# -------------------------------------------------
async def blacklisted_staff_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    try:
        bl_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(BLACKLISTED_STAFF_SHEET).get_all_values()
        )
        choices = []
        guild   = interaction.guild
        for row in bl_data[BL_DATA_START - 1:]:
            discord_id = safe_get(row, BL_DISCORD_ID_COL)
            if not discord_id or discord_id == "N/A":
                continue
            display = discord_id
            if guild:
                try:
                    member = guild.get_member(int(discord_id))
                    if member:
                        display = f"{member.display_name} ({discord_id})"
                except Exception:
                    pass
            if current.lower() in display.lower():
                choices.append(app_commands.Choice(name=display[:100], value=discord_id))
        return choices[:25]
    except Exception as e:
        print(f"[unblacklist] Autocomplete error: {e}")
        return []


# -------------------------------------------------
#  TIMETABLE COMMANDS
# -------------------------------------------------
@bot.tree.command(name="bookacademy", description="Book a slot on the Academy timetable")
@app_commands.describe(year="Select the year group", period="Select the period to book", subject="Enter the subject name", room="Enter the room number/name")
@app_commands.choices(year=[
    app_commands.Choice(name="Year 7",                value="Year 7"),
    app_commands.Choice(name="Year 8",                value="Year 8"),
    app_commands.Choice(name="Year 9",                value="Year 9"),
    app_commands.Choice(name="Year 10",               value="Year 10"),
    app_commands.Choice(name="Year 11",               value="Year 11"),
    app_commands.Choice(name="Additional Needs Unit", value="Additional Needs Unit"),
    app_commands.Choice(name="Isolation",             value="Isolation"),
    app_commands.Choice(name="Detention",             value="Detention"),
])
@cooldown()
async def book_academy(interaction: discord.Interaction, year: str, period: str, subject: str, room: str):
    await interaction.response.defer()

    if period in ("Form", "Lunch time") or year == "Detention":
        if not has_year_leader_role(interaction):
            await interaction.followup.send("❌ You need to be a Year Leader to book this slot.")
            return

    if not academy_current_message_id:
        await interaction.followup.send("❌ There is no active Academy timetable. Please wait for the daily post.")
        return

    teaching_name = await get_staff_teaching_name(str(interaction.user.id))
    if not teaching_name:
        await interaction.followup.send("❌ Could not find your staff profile. Please ensure your Discord account is linked.")
        return

    if year not in academy_timetable:
        await interaction.followup.send(f"❌ Invalid year group: {year}")
        return
    if period not in academy_timetable[year]:
        await interaction.followup.send(f"❌ Invalid period: {period}")
        return
    if academy_timetable[year][period]:
        await interaction.followup.send(f"❌ {year} - {period} is already booked by: {academy_timetable[year][period]}")
        return

    academy_timetable[year][period] = f"{teaching_name} - {subject} - {room}"

    try:
        ch = bot.get_channel(ACADEMY_TIMETABLE_CHANNEL_ID)
        if ch:
            msg = await ch.fetch_message(academy_current_message_id)
            await msg.edit(content=f"📅 **Academy Timetable - {datetime.now().strftime('%B %d, %Y')}**\n\n{format_timetable_message(academy_timetable)}")
    except Exception as e:
        print(f"[Timetable] Error updating Academy message: {e}")

    embed = discord.Embed(title="✅ Slot Booked!", color=discord.Color.green())
    embed.add_field(name="Year Group", value=year,          inline=True)
    embed.add_field(name="Period",     value=period,        inline=True)
    embed.add_field(name="Subject",    value=subject,       inline=True)
    embed.add_field(name="Room",       value=room,          inline=True)
    embed.add_field(name="Booked By",  value=teaching_name, inline=True)
    embed.set_footer(text="Academy Timetable")
    await interaction.followup.send(embed=embed)

@book_academy.autocomplete("period")
async def period_autocomplete_academy(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    try:
        year = next((opt.get('value') for opt in interaction.data.get('options', []) if opt.get('name') == 'year'), None)
        if not year or year not in academy_timetable:
            all_periods = sorted({p for y in academy_timetable.values() for p in y})
            return [app_commands.Choice(name=p, value=p) for p in all_periods if current.lower() in p.lower()][:25]
        available = get_available_periods(academy_timetable, year)
        return [app_commands.Choice(name=p, value=p) for p in available if current.lower() in p.lower()][:25]
    except Exception as e:
        print(f"Period autocomplete error: {e}")
        return []


@bot.tree.command(name="booksf", description="Book a slot on the Sixth Form timetable")
@app_commands.describe(year="Select the year group", period="Select the period to book", subject="Enter the subject name", room="Enter the room number/name")
@app_commands.choices(year=[
    app_commands.Choice(name="Year 12",               value="Year 12"),
    app_commands.Choice(name="Year 13",               value="Year 13"),
    app_commands.Choice(name="Additional Needs Unit", value="Additional Needs Unit"),
    app_commands.Choice(name="Isolation",             value="Isolation"),
    app_commands.Choice(name="Detention",             value="Detention"),
])
@cooldown()
async def book_sixth_form(interaction: discord.Interaction, year: str, period: str, subject: str, room: str):
    await interaction.response.defer()

    if period in ("Form", "Lunch time") or year == "Detention":
        if not has_year_leader_role(interaction):
            await interaction.followup.send("❌ You need to be a Year Leader to book this slot.")
            return

    if not sixth_form_current_message_id:
        await interaction.followup.send("❌ There is no active Sixth Form timetable. Please wait for the daily post.")
        return

    teaching_name = await get_staff_teaching_name(str(interaction.user.id))
    if not teaching_name:
        await interaction.followup.send("❌ Could not find your staff profile. Please ensure your Discord account is linked.")
        return

    if year not in sixth_form_timetable:
        await interaction.followup.send(f"❌ Invalid year group: {year}")
        return
    if period not in sixth_form_timetable[year]:
        await interaction.followup.send(f"❌ Invalid period: {period}")
        return
    if sixth_form_timetable[year][period]:
        await interaction.followup.send(f"❌ {year} - {period} is already booked by: {sixth_form_timetable[year][period]}")
        return

    sixth_form_timetable[year][period] = f"{teaching_name} - {subject} - {room}"

    try:
        ch = bot.get_channel(SIXTH_FORM_TIMETABLE_CHANNEL_ID)
        if ch:
            msg = await ch.fetch_message(sixth_form_current_message_id)
            await msg.edit(content=f"📅 **Sixth Form Timetable - {datetime.now().strftime('%B %d, %Y')}**\n\n{format_timetable_message(sixth_form_timetable)}")
    except Exception as e:
        print(f"[Timetable] Error updating Sixth Form message: {e}")

    embed = discord.Embed(title="✅ Slot Booked!", color=discord.Color.green())
    embed.add_field(name="Year Group", value=year,          inline=True)
    embed.add_field(name="Period",     value=period,        inline=True)
    embed.add_field(name="Subject",    value=subject,       inline=True)
    embed.add_field(name="Room",       value=room,          inline=True)
    embed.add_field(name="Booked By",  value=teaching_name, inline=True)
    embed.set_footer(text="Sixth Form Timetable")
    await interaction.followup.send(embed=embed)

@book_sixth_form.autocomplete("period")
async def period_autocomplete_sixth_form(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    try:
        year = next((opt.get('value') for opt in interaction.data.get('options', []) if opt.get('name') == 'year'), None)
        if not year or year not in sixth_form_timetable:
            all_periods = sorted({p for y in sixth_form_timetable.values() for p in y})
            return [app_commands.Choice(name=p, value=p) for p in all_periods if current.lower() in p.lower()][:25]
        available = get_available_periods(sixth_form_timetable, year)
        return [app_commands.Choice(name=p, value=p) for p in available if current.lower() in p.lower()][:25]
    except Exception as e:
        print(f"Period autocomplete error: {e}")
        return []


@bot.tree.command(name="removebookingacademy", description="Remove a booking from the Academy timetable")
@app_commands.describe(year="Select the year group", period="Select the period to remove")
@app_commands.choices(year=[
    app_commands.Choice(name="Year 7",                value="Year 7"),
    app_commands.Choice(name="Year 8",                value="Year 8"),
    app_commands.Choice(name="Year 9",                value="Year 9"),
    app_commands.Choice(name="Year 10",               value="Year 10"),
    app_commands.Choice(name="Year 11",               value="Year 11"),
    app_commands.Choice(name="Additional Needs Unit", value="Additional Needs Unit"),
    app_commands.Choice(name="Isolation",             value="Isolation"),
    app_commands.Choice(name="Detention",             value="Detention"),
])
@cooldown()
async def remove_booking_academy(interaction: discord.Interaction, year: str, period: str):
    await interaction.response.defer()

    if not academy_current_message_id:
        await interaction.followup.send("❌ There is no active Academy timetable.")
        return
    if year not in academy_timetable:
        await interaction.followup.send(f"❌ Invalid year group: {year}")
        return
    if period not in academy_timetable[year]:
        await interaction.followup.send(f"❌ Invalid period: {period}")
        return
    if not academy_timetable[year][period]:
        await interaction.followup.send(f"❌ {year} - {period} is not currently booked.")
        return

    old_booking = academy_timetable[year][period]
    is_admin    = has_timetable_admin_role(interaction)

    if not is_admin:
        teaching_name = await get_staff_teaching_name(str(interaction.user.id))
        if not teaching_name:
            await interaction.followup.send("❌ Could not find your staff profile.")
            return
        if not old_booking.startswith(f"{teaching_name} -"):
            await interaction.followup.send(f"❌ You can only remove your own bookings.\n\n**Current booking:** {old_booking}")
            return

    academy_timetable[year][period] = ""

    try:
        ch = bot.get_channel(ACADEMY_TIMETABLE_CHANNEL_ID)
        if ch:
            msg = await ch.fetch_message(academy_current_message_id)
            await msg.edit(content=f"📅 **Academy Timetable - {datetime.now().strftime('%B %d, %Y')}**\n\n{format_timetable_message(academy_timetable)}")
    except Exception as e:
        print(f"[Timetable] Error updating Academy message: {e}")

    embed = discord.Embed(title="🗑️ Booking Removed", color=discord.Color.orange())
    embed.add_field(name="Year Group",       value=year,        inline=True)
    embed.add_field(name="Period",           value=period,      inline=True)
    embed.add_field(name="Previous Booking", value=old_booking, inline=False)
    embed.set_footer(text="Academy Timetable | Removed by Admin" if is_admin else "Academy Timetable")
    await interaction.followup.send(embed=embed)

@remove_booking_academy.autocomplete("period")
async def period_autocomplete_remove_academy(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    try:
        year = next((opt.get('value') for opt in interaction.data.get('options', []) if opt.get('name') == 'year'), None)
        if not year or year not in academy_timetable:
            all_periods = sorted({p for y in academy_timetable.values() for p in y})
            return [app_commands.Choice(name=p, value=p) for p in all_periods if current.lower() in p.lower()][:25]
        booked = [p for p, b in academy_timetable[year].items() if b]
        return [app_commands.Choice(name=p, value=p) for p in booked if current.lower() in p.lower()][:25]
    except Exception as e:
        print(f"Period autocomplete error: {e}")
        return []


@bot.tree.command(name="removebookingsf", description="Remove a booking from the Sixth Form timetable")
@app_commands.describe(year="Select the year group", period="Select the period to remove")
@app_commands.choices(year=[
    app_commands.Choice(name="Year 12",               value="Year 12"),
    app_commands.Choice(name="Year 13",               value="Year 13"),
    app_commands.Choice(name="Additional Needs Unit", value="Additional Needs Unit"),
    app_commands.Choice(name="Isolation",             value="Isolation"),
    app_commands.Choice(name="Detention",             value="Detention"),
])
@cooldown()
async def remove_booking_sixth_form(interaction: discord.Interaction, year: str, period: str):
    await interaction.response.defer()

    if not sixth_form_current_message_id:
        await interaction.followup.send("❌ There is no active Sixth Form timetable.")
        return
    if year not in sixth_form_timetable:
        await interaction.followup.send(f"❌ Invalid year group: {year}")
        return
    if period not in sixth_form_timetable[year]:
        await interaction.followup.send(f"❌ Invalid period: {period}")
        return
    if not sixth_form_timetable[year][period]:
        await interaction.followup.send(f"❌ {year} - {period} is not currently booked.")
        return

    old_booking = sixth_form_timetable[year][period]
    is_admin    = has_timetable_admin_role(interaction)

    if not is_admin:
        teaching_name = await get_staff_teaching_name(str(interaction.user.id))
        if not teaching_name:
            await interaction.followup.send("❌ Could not find your staff profile.")
            return
        if not old_booking.startswith(f"{teaching_name} -"):
            await interaction.followup.send(f"❌ You can only remove your own bookings.\n\n**Current booking:** {old_booking}")
            return

    sixth_form_timetable[year][period] = ""

    try:
        ch = bot.get_channel(SIXTH_FORM_TIMETABLE_CHANNEL_ID)
        if ch:
            msg = await ch.fetch_message(sixth_form_current_message_id)
            await msg.edit(content=f"📅 **Sixth Form Timetable - {datetime.now().strftime('%B %d, %Y')}**\n\n{format_timetable_message(sixth_form_timetable)}")
    except Exception as e:
        print(f"[Timetable] Error updating Sixth Form message: {e}")

    embed = discord.Embed(title="🗑️ Booking Removed", color=discord.Color.orange())
    embed.add_field(name="Year Group",       value=year,        inline=True)
    embed.add_field(name="Period",           value=period,      inline=True)
    embed.add_field(name="Previous Booking", value=old_booking, inline=False)
    embed.set_footer(text="Sixth Form Timetable | Removed by Admin" if is_admin else "Sixth Form Timetable")
    await interaction.followup.send(embed=embed)

@remove_booking_sixth_form.autocomplete("period")
async def period_autocomplete_remove_sixth_form(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    try:
        year = next((opt.get('value') for opt in interaction.data.get('options', []) if opt.get('name') == 'year'), None)
        if not year or year not in sixth_form_timetable:
            all_periods = sorted({p for y in sixth_form_timetable.values() for p in y})
            return [app_commands.Choice(name=p, value=p) for p in all_periods if current.lower() in p.lower()][:25]
        booked = [p for p, b in sixth_form_timetable[year].items() if b]
        return [app_commands.Choice(name=p, value=p) for p in booked if current.lower() in p.lower()][:25]
    except Exception as e:
        print(f"Period autocomplete error: {e}")
        return []


@bot.tree.command(name="postacademy", description="Manually post the Academy timetable")
@cooldown()
async def post_academy_timetable(interaction: discord.Interaction):
    global academy_current_message_id, academy_timetable
    await interaction.response.defer(ephemeral=True)
    academy_timetable = _fresh_academy_timetable()
    try:
        ch = bot.get_channel(ACADEMY_TIMETABLE_CHANNEL_ID)
        if ch:
            now = datetime.now()
            msg = await ch.send(f"📅 **Academy Timetable - {now.strftime('%B %d, %Y')}**\n\n{format_timetable_message(academy_timetable)}")
            academy_current_message_id = msg.id
            await ch.send(f"<@&{ACADEMY_STAFF_ROLE_ID}> Let's get everything booked!")
            await interaction.followup.send(f"✅ Academy timetable posted! Message ID: {academy_current_message_id}", ephemeral=True)
        else:
            await interaction.followup.send("❌ Could not find the Academy timetable channel!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


@bot.tree.command(name="postsixthform", description="Manually post the Sixth Form timetable")
@cooldown()
async def post_sixth_form_timetable(interaction: discord.Interaction):
    global sixth_form_current_message_id, sixth_form_timetable
    await interaction.response.defer(ephemeral=True)
    sixth_form_timetable = _fresh_sixth_form_timetable()
    try:
        ch = bot.get_channel(SIXTH_FORM_TIMETABLE_CHANNEL_ID)
        if ch:
            now = datetime.now()
            msg = await ch.send(f"📅 **Sixth Form Timetable - {now.strftime('%B %d, %Y')}**\n\n{format_timetable_message(sixth_form_timetable)}")
            sixth_form_current_message_id = msg.id
            await ch.send(f"<@&{SIXTH_FORM_STAFF_ROLE_ID}> Let's get everything booked!")
            await interaction.followup.send(f"✅ Sixth Form timetable posted! Message ID: {sixth_form_current_message_id}", ephemeral=True)
        else:
            await interaction.followup.send("❌ Could not find the Sixth Form timetable channel!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


# -------------------------------------------------
#  /openroles
# -------------------------------------------------
@bot.tree.command(name="openroles", description="View which staff roles are open or filled")
@cooldown()
async def open_roles(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        roles_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(ROLES_LIST_SHEET).get_all_values()
        )

        all_roles = []
        for row in roles_data:
            for cell in row:
                cell = cell.strip()
                if cell and cell not in all_roles:
                    all_roles.append(cell)

        if not all_roles:
            await interaction.followup.send("❌ No roles found in the Roles List sheet.")
            return

        current_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(CURRENT_STAFF_SHEET).get_all_values()
        )

        role_counts: dict[str, int] = {}
        for row in current_data[CURRENT_STAFF_DATA_START - 1:]:
            if len(row) > 1:
                role = row[1].strip()
                if role:
                    role_counts[role] = role_counts.get(role, 0) + 1

        infinite_roles:   list[str] = []
        singleton_open:   list[str] = []
        singleton_filled: list[str] = []

        for role in all_roles:
            if role in INFINITE_ROLES:
                infinite_roles.append(role)
            elif role_counts.get(role, 0) == 0:
                singleton_open.append(role)
            else:
                singleton_filled.append(role)

        embed = discord.Embed(
            title="📋 Staff Role Openings",
            color=discord.Color.blue(),
            timestamp=datetime.now(),
        )

        def add_chunked_field(embed, label_first, label_cont, items):
            chunk, chunks = "", []
            for item in items:
                line = f"• {item}\n"
                if len(chunk) + len(line) > 1000:
                    chunks.append(chunk.strip())
                    chunk = ""
                chunk += line
            if chunk:
                chunks.append(chunk.strip())
            for i, c in enumerate(chunks):
                embed.add_field(name=label_first if i == 0 else label_cont, value=c, inline=False)

        if singleton_open:
            add_chunked_field(embed,
                f"✅ Open Positions ({len(singleton_open)})",
                "✅ Open Positions (cont.)",
                singleton_open)
        else:
            embed.add_field(name="✅ Open Positions", value="*All positions are currently filled.*", inline=False)

        if infinite_roles:
            embed.add_field(
                name="♾️ Always Hiring (Unlimited Spots)",
                value="\n".join(f"• {r}" for r in infinite_roles),
                inline=False,
            )

        if singleton_filled:
            add_chunked_field(embed,
                f"❌ Filled Positions ({len(singleton_filled)})",
                "❌ Filled Positions (cont.)",
                singleton_filled)

        embed.set_footer(
            text=f"{len(singleton_open)} open · {len(singleton_filled)} filled · {len(infinite_roles)} unlimited"
        )
        await interaction.followup.send(embed=embed)

    except gspread.WorksheetNotFound:
        await interaction.followup.send(f"❌ Sheet '{ROLES_LIST_SHEET}' not found!")
    except Exception as e:
        await interaction.followup.send(f"❌ Unexpected error: {e}")


# -------------------------------------------------
#  /status
# -------------------------------------------------
@bot.tree.command(name="status", description="Check bot health and self-ping statistics")
@cooldown()
async def bot_status(interaction: discord.Interaction):
    embed = discord.Embed(title="Bot Status", color=discord.Color.blue(), timestamp=datetime.now())
    embed.add_field(name="Bot Name",        value=bot.user.name,                    inline=True)
    embed.add_field(name="Servers",         value=len(bot.guilds),                  inline=True)
    embed.add_field(name="Current Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)

    if last_ping_time:
        time_since = (datetime.now() - last_ping_time).total_seconds()
        embed.add_field(name="Last Health Check",     value=f"{int(time_since)}s ago", inline=True)
        embed.add_field(name="Last Recorded Latency", value=f"{last_ping_latency}ms",  inline=True)
    else:
        embed.add_field(name="Last Health Check",     value="N/A", inline=True)
        embed.add_field(name="Last Recorded Latency", value="N/A", inline=True)

    embed.add_field(name="Failed Checks",  value=ping_failures,            inline=True)
    embed.add_field(name="Check Interval", value=f"{SELF_PING_INTERVAL}s", inline=True)
    embed.add_field(name="Google Sheets",  value="Connected" if sheets_client else "Disconnected", inline=True)
    embed.add_field(name="Roblox Cookie",  value="✅ Set" if ROBLOX_COOKIE else "❌ Missing", inline=True)

    if ping_failures == 0 and bot.latency < 1:
        embed.add_field(name="Overall Status", value="✅ Healthy",        inline=False)
        embed.color = discord.Color.green()
    elif ping_failures < 3 and bot.latency < 2:
        embed.add_field(name="Overall Status", value="⚠️ Degraded",       inline=False)
        embed.color = discord.Color.orange()
    else:
        embed.add_field(name="Overall Status", value="❌ Issues Detected", inline=False)
        embed.color = discord.Color.red()

    await interaction.response.send_message(embed=embed)


# -------------------------------------------------
#  /edit
# -------------------------------------------------
@bot.tree.command(name="edit", description="Edit a staff member's details")
@app_commands.describe(staff_name="Select the staff member to edit", field="Which field to update", value="New value for the field")
@app_commands.choices(field=[
    app_commands.Choice(name="Role",            value="role"),
    app_commands.Choice(name="Roblox Username", value="roblox_username"),
    app_commands.Choice(name="Teaching Name",   value="teaching_name"),
    app_commands.Choice(name="Area",            value="area"),
    app_commands.Choice(name="Discord User ID", value="discord_id"),
])
@app_commands.autocomplete(staff_name=edit_staff_autocomplete, value=edit_value_autocomplete)
@cooldown()
async def edit_staff(interaction: discord.Interaction, staff_name: str, field: str, value: str):
    await interaction.response.defer()
    field_label, target_cell = FIELD_MAP.get(field, (field, "?"))

    if not value:
        await interaction.followup.send(f"A value is required when editing **{field_label}**.")
        return

    params = {"action": "edit", "staffName": staff_name, "field": field, "cell": target_cell, "value": value}

    try:
        status, response_text = await safe_apps_script_get(APPS_SCRIPT_URL, params)

        if status == 200 and "error" not in response_text.lower():
            embed = discord.Embed(title="Staff Record Updated!", color=discord.Color.green())
            embed.add_field(name="Staff Member",  value=staff_name,                       inline=True)
            embed.add_field(name="Field Updated", value=f"{field_label} ({target_cell})", inline=True)
            embed.add_field(name="New Value",     value=value,                            inline=True)
            embed.set_footer(text="Staff record has been updated")

            if field == "role":
                roblox_username = await get_roblox_username_for_staff(staff_name)
                if roblox_username:
                    success, result = await roblox_set_rank_by_sheet_role(roblox_username, value)
                    if success:
                        embed.add_field(name="Roblox Group", value=f"✅ Ranked to **{result}**", inline=False)
                    else:
                        embed.add_field(name="Roblox Group", value=f"⚠️ {result}", inline=False)
                else:
                    embed.add_field(name="Roblox Group", value="⚠️ No Roblox username on file", inline=False)

            guild = interaction.guild
            if guild:
                if field == "discord_id":
                    target_discord_id = value
                else:
                    target_discord_id = await get_discord_id_for_staff(staff_name)

                if target_discord_id and target_discord_id != "N/A":
                    member = await get_discord_member_by_id(guild, target_discord_id)
                    if member:
                        if field == "role":
                            effective_role          = value
                            effective_teaching_name = staff_name
                        elif field == "teaching_name":
                            effective_teaching_name = value
                            effective_role          = await get_role_for_staff(staff_name)
                        elif field == "discord_id":
                            effective_role          = await get_role_for_staff(staff_name)
                            effective_teaching_name = staff_name
                        else:
                            effective_role          = await get_role_for_staff(staff_name)
                            effective_teaching_name = staff_name

                        if effective_role and effective_role != "N/A":
                            await apply_discord_roles_and_nick(
                                guild, member,
                                effective_role,
                                effective_teaching_name or staff_name,
                                embed=embed,
                            )
                        else:
                            embed.add_field(name="Discord", value="⚠️ Could not determine role for Discord update", inline=False)
                    else:
                        embed.add_field(name="Discord", value="⚠️ Member not found in this server", inline=False)
                else:
                    embed.add_field(name="Discord", value="⚠️ No Discord ID on file — skipped role/nick update", inline=False)

            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"Apps Script error (HTTP {status}):\n```{response_text[:500]}```")
    except aiohttp.ClientError as e:
        await interaction.followup.send(f"Could not reach the Apps Script web app: {e}")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")


# -------------------------------------------------
#  /strike
# -------------------------------------------------
@bot.tree.command(name="strike", description="Issue a strike to a staff member with a required reason")
@app_commands.describe(staff_name="Select the staff member", reason="Reason for the strike (required)")
@app_commands.autocomplete(staff_name=edit_staff_autocomplete)
@cooldown()
async def issue_strike(interaction: discord.Interaction, staff_name: str, reason: str):
    await interaction.response.defer()

    if not reason or not reason.strip():
        await interaction.followup.send("❌ A reason is required when issuing a strike.")
        return

    params = {"action": "strike", "staffName": staff_name, "strikeReason": reason.strip()}

    try:
        status, response_text = await safe_apps_script_get(APPS_SCRIPT_URL, params)

        if status == 200 and "error" not in response_text.lower():
            new_strikes = "updated"
            if "new_strikes:" in response_text:
                try:
                    new_strikes = response_text.split("new_strikes:")[1].strip()
                except Exception:
                    pass

            embed = discord.Embed(title="⚠️ Strike Issued", color=discord.Color.orange())
            embed.add_field(name="Staff Member", value=staff_name,  inline=True)
            embed.add_field(name="New Strikes",  value=new_strikes, inline=True)
            embed.add_field(name="Reason",       value=reason,      inline=False)
            embed.set_footer(text="Strike has been recorded")
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"Apps Script error (HTTP {status}):\n```{response_text[:500]}```")
    except aiohttp.ClientError as e:
        await interaction.followup.send(f"Could not reach the Apps Script web app: {e}")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")


# -------------------------------------------------
#  /revokestrike
# -------------------------------------------------
@bot.tree.command(name="revokestrike", description="Revoke the most recent strike from a staff member")
@app_commands.describe(staff_name="Select the staff member")
@app_commands.autocomplete(staff_name=edit_staff_autocomplete)
@cooldown()
async def revoke_strike(interaction: discord.Interaction, staff_name: str):
    await interaction.response.defer()

    params = {"action": "revokestrike", "staffName": staff_name}

    try:
        status, response_text = await safe_apps_script_get(APPS_SCRIPT_URL, params)

        if status == 200 and "error" not in response_text.lower():
            new_strikes    = "updated"
            revoked_reason = "N/A"
            if "new_strikes:" in response_text:
                try:
                    parts = response_text.split("new_strikes:")[1].strip().split("revoked_reason:", 1)
                    new_strikes = parts[0].strip()
                    if len(parts) > 1:
                        revoked_reason = parts[1].strip()
                except Exception:
                    pass

            embed = discord.Embed(title="✅ Strike Revoked", color=discord.Color.green())
            embed.add_field(name="Staff Member",      value=staff_name,     inline=True)
            embed.add_field(name="Remaining Strikes", value=new_strikes,    inline=True)
            embed.add_field(name="Revoked Strike",    value=revoked_reason, inline=False)
            embed.set_footer(text="Most recent strike has been removed")
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"Apps Script error (HTTP {status}):\n```{response_text[:500]}```")
    except aiohttp.ClientError as e:
        await interaction.followup.send(f"Could not reach the Apps Script web app: {e}")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")


# -------------------------------------------------
#  /loa
# -------------------------------------------------
@bot.tree.command(name="loa", description="Set a Leave of Absence for a staff member")
@app_commands.describe(staff_name="Select the staff member", start_date="Start date (e.g. 2024-01-15)", end_date="End date (e.g. 2024-01-30)", reason="Reason for the leave of absence")
@app_commands.autocomplete(staff_name=edit_staff_autocomplete)
@cooldown()
async def set_loa(interaction: discord.Interaction, staff_name: str, start_date: str, end_date: str, reason: str):
    await interaction.response.defer()

    if not start_date.strip() or not end_date.strip() or not reason.strip():
        await interaction.followup.send("❌ Start date, end date, and reason are all required.")
        return

    loa_value = f"{start_date.strip()} - {end_date.strip()}"
    params    = {"action": "loa", "staffName": staff_name, "loaValue": loa_value, "loaReason": reason.strip()}

    try:
        status, response_text = await safe_apps_script_get(APPS_SCRIPT_URL, params)

        if status == 200 and "error" not in response_text.lower():
            embed = discord.Embed(title="📅 Leave of Absence Set", color=discord.Color.blue())
            embed.add_field(name="Staff Member", value=staff_name, inline=True)
            embed.add_field(name="Start Date",   value=start_date, inline=True)
            embed.add_field(name="End Date",     value=end_date,   inline=True)
            embed.add_field(name="LOA Period",   value=loa_value,  inline=False)
            embed.add_field(name="Reason",       value=reason,     inline=False)
            embed.set_footer(text="LOA has been recorded")
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"Apps Script error (HTTP {status}):\n```{response_text[:500]}```")
    except aiohttp.ClientError as e:
        await interaction.followup.send(f"Could not reach the Apps Script web app: {e}")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")


# -------------------------------------------------
#  /endloa
# -------------------------------------------------
@bot.tree.command(name="endloa", description="End a staff member's Leave of Absence")
@app_commands.describe(staff_name="Select the staff member")
@app_commands.autocomplete(staff_name=edit_staff_autocomplete)
@cooldown()
async def end_loa(interaction: discord.Interaction, staff_name: str):
    await interaction.response.defer()

    params = {"action": "endloa", "staffName": staff_name}

    try:
        status, response_text = await safe_apps_script_get(APPS_SCRIPT_URL, params)

        if status == 200 and "error" not in response_text.lower():
            previous_loa = "N/A"
            if "previous_loa:" in response_text:
                try:
                    previous_loa = response_text.split("previous_loa:")[1].strip()
                except Exception:
                    pass

            embed = discord.Embed(title="✅ Leave of Absence Ended", color=discord.Color.green())
            embed.add_field(name="Staff Member", value=staff_name,   inline=True)
            embed.add_field(name="Previous LOA", value=previous_loa, inline=True)
            embed.add_field(name="Status",       value="Active",     inline=True)
            embed.set_footer(text="LOA ended")
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"Apps Script error (HTTP {status}):\n```{response_text[:500]}```")
    except aiohttp.ClientError as e:
        await interaction.followup.send(f"Could not reach the Apps Script web app: {e}")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")


# -------------------------------------------------
#  /logtraining
# -------------------------------------------------
@bot.tree.command(name="logtraining", description="Log a staff member's training level completion")
@app_commands.describe(staff_name="Select the staff member", training_level="The training level they completed")
@app_commands.autocomplete(staff_name=edit_staff_autocomplete)
@cooldown()
async def log_training(interaction: discord.Interaction, staff_name: str, training_level: str):
    await interaction.response.defer()

    try:
        all_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(CURRENT_STAFF_SHEET).get_all_values()
        )

        current_level = None
        for row in all_data[CURRENT_STAFF_DATA_START - 1:]:
            if len(row) > CURRENT_STAFF_NAME_COL and row[CURRENT_STAFF_NAME_COL].strip().lower() == staff_name.lower():
                current_level = safe_get(row, 5, "").strip()
                break

        if current_level is None:
            await interaction.followup.send(f"❌ Staff member **{staff_name}** not found.")
            return
        if current_level == "Level 3":
            await interaction.followup.send(f"❌ **{staff_name}** has already completed Level 3.")
            return
        if current_level == "Level 1" and training_level not in ["Level 2", "Level 3"]:
            await interaction.followup.send(f"❌ **{staff_name}** currently has Level 1. They can only progress to Level 2 or 3.")
            return
        if current_level == "Level 2" and training_level != "Level 3":
            await interaction.followup.send(f"❌ **{staff_name}** currently has Level 2. They can only progress to Level 3.")
            return

        params = {"action": "logtraining", "staffName": staff_name, "trainingLevel": training_level}
        status, response_text = await safe_apps_script_get(APPS_SCRIPT_URL, params)

        if status == 200 and "error" not in response_text.lower():
            embed = discord.Embed(title="📚 Training Logged", color=discord.Color.blue())
            embed.add_field(name="Staff Member",   value=staff_name,              inline=True)
            embed.add_field(name="Previous Level", value=current_level or "None", inline=True)
            embed.add_field(name="New Level",      value=training_level,          inline=True)
            embed.set_footer(text="Training level updated")
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"Apps Script error (HTTP {status}):\n```{response_text[:500]}```")

    except gspread.WorksheetNotFound:
        await interaction.followup.send(f"❌ Sheet '{CURRENT_STAFF_SHEET}' not found!")
    except aiohttp.ClientError as e:
        await interaction.followup.send(f"❌ Could not reach Apps Script: {e}")
    except Exception as e:
        await interaction.followup.send(f"❌ Unexpected error: {e}")

@log_training.autocomplete("training_level")
async def training_level_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    choices = [
        app_commands.Choice(name="Level 1", value="Level 1"),
        app_commands.Choice(name="Level 2", value="Level 2"),
        app_commands.Choice(name="Level 3", value="Level 3"),
    ]
    if current:
        choices = [c for c in choices if current.lower() in c.name.lower()] or choices
    return choices

# -------------------------------------------------
#  HIRE HELPER  (shared by /hire and override button)
# -------------------------------------------------
async def _execute_hire(
    interaction: discord.Interaction,
    teaching_name: str,
    roblox_username: str,
    discord_account: discord.Member,
    area: str,
    role: str,
) -> None:
    discord_user_id = str(discord_account.id)

    params = {
        "action":        "hire",
        "teachingName":  teaching_name,
        "staffUsername": roblox_username,
        "area":          area,
        "position":      role,
        "discordId":     discord_user_id,
    }

    try:
        status, response_text = await safe_apps_script_get(APPS_SCRIPT_URL, params)

        if status == 200 and "error" not in response_text.lower():
            embed = discord.Embed(title="✅ New Staff Member Hired!", color=discord.Color.green())
            embed.add_field(name="Teaching Name",   value=teaching_name,           inline=True)
            embed.add_field(name="Roblox Username", value=roblox_username,         inline=True)
            embed.add_field(name="Discord Account", value=discord_account.mention, inline=True)
            embed.add_field(name="Discord ID",      value=discord_user_id,         inline=True)
            embed.add_field(name="Area",            value=area,                    inline=True)
            embed.add_field(name="Role",            value=role,                    inline=True)
            embed.set_footer(text="Staff record has been created")

            success, result = await roblox_set_rank_by_sheet_role(roblox_username, role)
            if success:
                embed.add_field(name="Roblox Group", value=f"✅ Ranked to **{result}**", inline=False)
            else:
                embed.add_field(name="Roblox Group", value=f"⚠️ {result}", inline=False)

            guild = interaction.guild
            if guild:
                await apply_discord_roles_and_nick(
                    guild, discord_account,
                    role, teaching_name,
                    embed=embed,
                )

            try:
                await discord_account.send(
                    "## ❗ | Congratulations on Your Appointment at Winstree Academy\n"
                    "We are pleased to welcome you to the staff team at Winstree Academy.\n\n"
                    "***__Important Information__***\n"
                    "- You are required to complete your Initial Teacher Training within one week of receiving this message.\n"
                    "- All staff members are expected to attend four sessions per week (Sunday–Saturday).\n"
                    "- High standards of grammar, punctuation, and spelling (SPaG) must be maintained at all times while on school grounds.\n"
                    "- Staff sessions begin at 19:45 (UK time) and conclude at 21:10.\n\n"
                    "Your teaching name and assigned roles have already been recorded. Please run /profile in the server to review your details.\n\n"
                    "We look forward to your attendance at today's session, commencing at 19:45 BST in the briefing room.\n\n"
                    "*Senior Leadership Team*\n"
                    "**Winstree Academy**"
                )
                embed.add_field(name="Welcome DM", value="✅ Sent", inline=False)
            except discord.Forbidden:
                embed.add_field(name="Welcome DM", value="⚠️ Could not send DM (user may have DMs disabled)", inline=False)
            except Exception as e:
                embed.add_field(name="Welcome DM", value=f"⚠️ DM error: {e}", inline=False)

            refresh_staff_names_cache()
            await interaction.followup.send(embed=embed)

        else:
            await interaction.followup.send(
                f"Apps Script error (HTTP {status}):\n```{response_text[:500]}```"
            )

    except aiohttp.ClientError as e:
        await interaction.followup.send(f"Could not reach Apps Script: {e}")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")


# -------------------------------------------------
#  HIRE OVERRIDE VIEW
# -------------------------------------------------
class HireOverrideView(discord.ui.View):
    def __init__(
        self,
        hiring_staff: discord.Member,
        teaching_name: str,
        roblox_username: str,
        discord_account: discord.Member,
        area: str,
        role: str,
        block_reason: str,
    ):
        super().__init__(timeout=300)
        self.hiring_staff    = hiring_staff
        self.teaching_name   = teaching_name
        self.roblox_username = roblox_username
        self.discord_account = discord_account
        self.area            = area
        self.role            = role
        self.block_reason    = block_reason

    async def _disable_all(self, interaction: discord.Interaction, new_title: str, new_color: discord.Color) -> None:
        for child in self.children:
            child.disabled = True
        embed = interaction.message.embeds[0]
        embed.title = new_title
        embed.color = new_color
        await interaction.message.edit(embed=embed, view=self)

    @discord.ui.button(label="Override — Hire Anyway", style=discord.ButtonStyle.danger, emoji="⚠️", custom_id="hire_override_yes")
    async def override(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._disable_all(
            interaction,
            "⚠️ Override Approved — Hiring...",
            discord.Color.orange(),
        )

        override_note = discord.Embed(
            title="⚠️ Eligibility Override Used",
            description=(
                f"{interaction.user.mention} overrode the eligibility block and proceeded with hiring "
                f"{self.discord_account.mention}."
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(),
        )
        override_note.add_field(name="Originally Attempted By", value=self.hiring_staff.mention, inline=True)
        override_note.add_field(name="Overridden By",           value=interaction.user.mention,  inline=True)
        override_note.add_field(name="Teaching Name",           value=self.teaching_name,        inline=True)
        override_note.add_field(name="Roblox Username",         value=self.roblox_username,      inline=True)
        override_note.add_field(name="Area",                    value=self.area,                 inline=True)
        override_note.add_field(name="Role",                    value=self.role,                 inline=True)
        override_note.add_field(name="Block Reason",            value=self.block_reason,         inline=False)
        override_note.set_footer(text=f"Target Discord ID: {self.discord_account.id}")

        try:
            await interaction.message.channel.send(embed=override_note)
        except Exception as e:
            print(f"[Hire] Failed to send override log: {e}")

        await _execute_hire(
            interaction,
            self.teaching_name,
            self.roblox_username,
            self.discord_account,
            self.area,
            self.role,
        )

    @discord.ui.button(label="No Override — Cancel", style=discord.ButtonStyle.secondary, emoji="❌", custom_id="hire_override_no")
    async def no_override(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._disable_all(
            interaction,
            "❌ Hire Cancelled",
            discord.Color.red(),
        )
        await interaction.followup.send(
            f"Hire cancelled. {self.discord_account.mention} was **not** added to the staff team.",
            ephemeral=True,
        )

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


# -------------------------------------------------
#  /hire
# -------------------------------------------------
@bot.tree.command(name="hire", description="Hire a new staff member")
@app_commands.describe(
    teaching_name="The staff member's teaching name",
    roblox_username="The staff member's Roblox username",
    discord_account="Select the Discord user",
    area="Academy or Sixth Form",
    role="Their role from the Roles List",
)
@app_commands.choices(area=[
    app_commands.Choice(name="Academy",    value="Academy"),
    app_commands.Choice(name="Sixth Form", value="Sixth Form"),
])
@app_commands.autocomplete(role=position_autocomplete)
@cooldown()
async def hire(
    interaction: discord.Interaction,
    teaching_name: str,
    roblox_username: str,
    discord_account: discord.Member,
    area: str,
    role: str,
):
    await interaction.response.defer(ephemeral=True)

    discord_user_id = str(discord_account.id)

    can_hire, block_reason = await check_application_eligibility(discord_user_id)

    if not can_hire:
        if block_reason == "blacklisted":
            reason_label = "🚫 User is on the staff blacklist"
            embed_color  = discord.Color.red()
        elif block_reason == "recent_staff":
            reason_label = "⏳ User was removed from staff within the last 2 weeks"
            embed_color  = discord.Color.orange()
        else:
            reason_label = f"Unknown (`{block_reason}`)"
            embed_color  = discord.Color.greyple()

        # Quiet message to the person who ran the command
        await interaction.followup.send(
            "Whoops! Looks like you can't hire them right now. Try again later!",
            ephemeral=True,
        )

        # Override prompt to the log channel
        block_embed = discord.Embed(
            title="⛔ Hire Blocked — Eligibility Check Failed",
            description=(
                f"{interaction.user.mention} attempted to hire {discord_account.mention} "
                f"but they did not pass the eligibility check.\n\n"
                f"Use the buttons below to override or cancel."
            ),
            color=embed_color,
            timestamp=datetime.now(),
        )
        block_embed.add_field(name="Attempted By",    value=interaction.user.mention, inline=True)
        block_embed.add_field(name="Target User",     value=discord_account.mention,  inline=True)
        block_embed.add_field(name="Discord ID",      value=discord_user_id,          inline=True)
        block_embed.add_field(name="Teaching Name",   value=teaching_name,            inline=True)
        block_embed.add_field(name="Roblox Username", value=roblox_username,          inline=True)
        block_embed.add_field(name="Area",            value=area,                     inline=True)
        block_embed.add_field(name="Role",            value=role,                     inline=True)
        block_embed.add_field(name="Reason Blocked",  value=reason_label,             inline=False)
        block_embed.set_footer(text=f"Attempted by {interaction.user.display_name} · {interaction.user.id}")

        view = HireOverrideView(
            hiring_staff=interaction.user,
            teaching_name=teaching_name,
            roblox_username=roblox_username,
            discord_account=discord_account,
            area=area,
            role=role,
            block_reason=reason_label,
        )

        log_channel = interaction.client.get_channel(BLOCKED_APP_LOG_CHANNEL_ID)
        if log_channel:
            try:
                await log_channel.send(embed=block_embed, view=view)
            except Exception as e:
                print(f"[Hire] Failed to send block notification: {e}")
        return

    # No block — proceed normally
    await _execute_hire(interaction, teaching_name, roblox_username, discord_account, area, role)


# -------------------------------------------------
#  /removestaff
# -------------------------------------------------
@bot.tree.command(name="removestaff", description="Remove a staff member and record their departure")
@app_commands.describe(staff_name="Select the staff member to remove", reason="Reason for departure")
@app_commands.autocomplete(staff_name=edit_staff_autocomplete)
@cooldown()
async def remove_staff(interaction: discord.Interaction, staff_name: str, reason: str):
    await interaction.response.defer()
    current_date = datetime.now().strftime("%Y-%m-%d")

    roblox_username   = await get_roblox_username_for_staff(staff_name)
    target_discord_id = await get_discord_id_for_staff(staff_name)

    params = {
        "action":          "removestaff",
        "staffName":       staff_name,
        "departureDate":   current_date,
        "departureReason": reason,
    }

    try:
        status, response_text = await safe_apps_script_get(APPS_SCRIPT_URL, params)

        if status == 200 and "error" not in response_text.lower():
            embed = discord.Embed(title="Staff Member Removed", color=discord.Color.orange())
            embed.add_field(name="Staff Member",   value=staff_name,   inline=True)
            embed.add_field(name="Departure Date", value=current_date, inline=True)
            embed.add_field(name="Reason",         value=reason,       inline=False)
            embed.set_footer(text="Staff record has been updated")

            # Write the Discord ID to Column E of the Remove Staff Log
            if target_discord_id and target_discord_id != "N/A":
                try:
                    def _write_discord_id_to_rsl():
                        ws       = spreadsheet.worksheet(REMOVE_STAFF_LOG_SHEET)
                        all_data = ws.get_all_values()
                        # Search bottom-up for the most recent matching entry
                        for i in range(len(all_data) - 1, RSL_DATA_START - 2, -1):
                            if safe_get(all_data[i], RSL_NAME_COL).lower() == staff_name.lower():
                                ws.update_cell(i + 1, 5, target_discord_id)  # Col E = 5 (1-indexed)
                                return True
                        return False

                    wrote = await safe_sheets_call(_write_discord_id_to_rsl)
                    if wrote:
                        embed.add_field(name="RSL Discord ID", value=f"✅ Written to Column E: `{target_discord_id}`", inline=False)
                    else:
                        embed.add_field(name="RSL Discord ID", value="⚠️ Could not find matching row in Remove Staff Log", inline=False)
                except Exception as e:
                    print(f"[removestaff] Discord ID RSL write error: {e}")
                    embed.add_field(name="RSL Discord ID", value=f"⚠️ Write error: {e}", inline=False)
            else:
                embed.add_field(name="RSL Discord ID", value="➖ No Discord ID on file — skipped", inline=False)

            if roblox_username:
                success, err = await roblox_demote_to_rank_1(roblox_username)
                if success:
                    embed.add_field(name="Roblox Group", value="✅ Demoted to rank 1", inline=False)
                else:
                    embed.add_field(name="Roblox Group", value=f"⚠️ {err}", inline=False)
            else:
                embed.add_field(name="Roblox Group", value="⚠️ No Roblox username on file", inline=False)

            guild = interaction.guild
            if guild and target_discord_id and target_discord_id != "N/A":
                member = await get_discord_member_by_id(guild, target_discord_id)
                if member:
                    await clear_discord_roles_and_reset_nick(
                        guild, member,
                        roblox_username or staff_name,
                        embed=embed,
                    )
                else:
                    embed.add_field(name="Discord", value="⚠️ Member not found in this server", inline=False)
            else:
                embed.add_field(name="Discord", value="⚠️ No Discord ID on file — skipped role/nick update", inline=False)

            refresh_staff_names_cache()
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"Apps Script error (HTTP {status}):\n```{response_text[:500]}```")
    except aiohttp.ClientError as e:
        await interaction.followup.send(f"Could not reach Apps Script: {e}")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")


# -------------------------------------------------
#  /profile
# -------------------------------------------------
@bot.tree.command(name="profile", description="View your own staff profile")
@cooldown()
async def profile(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_discord_id = str(interaction.user.id)

    try:
        all_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(CURRENT_STAFF_SHEET).get_all_values()
        )

        staff_row_idx  = None
        staff_row_data = None
        for i, row in enumerate(all_data[CURRENT_STAFF_DATA_START - 1:], start=CURRENT_STAFF_DATA_START):
            if len(row) > 7 and row[7].strip() == user_discord_id:
                staff_row_idx  = i
                staff_row_data = row
                break

        if staff_row_data is None:
            await interaction.followup.send(
                f"No staff profile found for your Discord account.\nYour Discord ID: `{user_discord_id}`\n\nIf you believe this is an error, please contact an administrator.",
                ephemeral=True
            )
            return

        teaching_name = safe_get(staff_row_data, 3)
        loa_value     = safe_get(staff_row_data, 6)
        strikes_value = safe_get(staff_row_data, 8, "0")

        embed = discord.Embed(title="Your Staff Profile", description=f"**{teaching_name}**", color=discord.Color.blue())
        embed.add_field(name="Role",            value=safe_get(staff_row_data, 1),      inline=True)
        embed.add_field(name="Roblox Username", value=safe_get(staff_row_data, 2),      inline=True)
        embed.add_field(name="Area",            value=safe_get(staff_row_data, 4),      inline=True)
        embed.add_field(name="Training Level",  value=safe_get(staff_row_data, 5, "0"), inline=True)
        embed.add_field(name="LOA",             value=loa_value,                        inline=True)
        embed.add_field(name="Strikes",         value=strikes_value,                    inline=True)

        try:
            if staff_row_idx:
                ws = spreadsheet.worksheet(CURRENT_STAFF_SHEET)

                strike_cell = await safe_sheets_call(lambda: ws.cell(staff_row_idx, 9))
                if strike_cell.note:
                    lines = [l.strip() for l in strike_cell.note.strip().split('\n') if l.strip()]
                    formatted = []
                    for line in lines:
                        if line.startswith("Strike "):
                            parts = line.split(" - ", 1)
                            formatted.append(f"{parts[0].replace('Strike ', '')} - {parts[1]}" if len(parts) == 2 else line)
                        else:
                            formatted.append(line)
                    if formatted:
                        embed.add_field(name="Strike Reasons", value="\n".join(formatted), inline=False)

                if loa_value != "N/A":
                    loa_cell = await safe_sheets_call(lambda: ws.cell(staff_row_idx, 7))
                    if loa_cell.note:
                        embed.add_field(name="LOA Reason", value=loa_cell.note, inline=False)
        except Exception as e:
            print(f"Error fetching cell notes: {e}")

        embed.add_field(name="Attendance", value=f"{safe_get(staff_row_data, 9, '0')} sessions", inline=False)
        embed.set_footer(text=f"Discord ID: {user_discord_id}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    except gspread.WorksheetNotFound:
        await interaction.followup.send(f"Sheet '{CURRENT_STAFF_SHEET}' not found!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}", ephemeral=True)


# -------------------------------------------------
#  /attendance
# -------------------------------------------------
@bot.tree.command(name="attendance", description="Mark attendance for a staff member")
@app_commands.describe(staff_name="Select the staff member")
@app_commands.autocomplete(staff_name=edit_staff_autocomplete)
@cooldown()
async def mark_attendance(interaction: discord.Interaction, staff_name: str):
    await interaction.response.defer()

    params = {"action": "attendance", "staffName": staff_name}

    try:
        status, response_text = await safe_apps_script_get(APPS_SCRIPT_URL, params)

        if status == 200 and "error" not in response_text.lower():
            embed = discord.Embed(title="Attendance Marked!", color=discord.Color.green())
            embed.add_field(name="Staff Member", value=staff_name,        inline=True)
            embed.add_field(name="Attendance",   value="1 session added", inline=True)
            embed.set_footer(text="Attendance has been updated")
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"Apps Script error (HTTP {status}):\n```{response_text[:500]}```")
    except aiohttp.ClientError as e:
        await interaction.followup.send(f"Could not reach Apps Script: {e}")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")


# -------------------------------------------------
#  /info
# -------------------------------------------------
@bot.tree.command(name="info", description="Get information about a staff member")
@app_commands.describe(staff_name="Select the staff member")
@app_commands.autocomplete(staff_name=edit_staff_autocomplete)
@cooldown()
async def staff_info(interaction: discord.Interaction, staff_name: str):
    await interaction.response.defer()

    try:
        all_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(CURRENT_STAFF_SHEET).get_all_values()
        )

        staff_row_idx  = None
        staff_row_data = None
        for i, row in enumerate(all_data[CURRENT_STAFF_DATA_START - 1:], start=CURRENT_STAFF_DATA_START):
            if len(row) > CURRENT_STAFF_NAME_COL and row[CURRENT_STAFF_NAME_COL].strip().lower() == staff_name.lower():
                staff_row_idx  = i
                staff_row_data = row
                break

        if staff_row_data is None:
            await interaction.followup.send(f"Staff member **{staff_name}** not found.")
            return

        strikes_value = safe_get(staff_row_data, 8, "0")
        loa_value     = safe_get(staff_row_data, 6)
        discord_id    = safe_get(staff_row_data, 7)

        embed = discord.Embed(title=f"Staff Information: {staff_name}", color=discord.Color.blue())
        embed.add_field(name="Role",                value=safe_get(staff_row_data, 1),      inline=True)
        embed.add_field(name="Roblox Username",     value=safe_get(staff_row_data, 2),      inline=True)
        embed.add_field(name="Area",                value=safe_get(staff_row_data, 4),      inline=True)
        embed.add_field(name="TL (Training Level)", value=safe_get(staff_row_data, 5, "0"), inline=True)
        embed.add_field(name="LOA",                 value=loa_value,                        inline=True)
        embed.add_field(name="Discord",             value=f"<@{discord_id}>" if discord_id != "N/A" else "Not set", inline=True)
        embed.add_field(name="Strikes",             value=strikes_value,                    inline=True)
        embed.add_field(name="Attendance",          value=f"{safe_get(staff_row_data, 9, '0')} sessions", inline=True)

        try:
            if staff_row_idx:
                ws = spreadsheet.worksheet(CURRENT_STAFF_SHEET)

                strike_cell = await safe_sheets_call(lambda: ws.cell(staff_row_idx, 9))
                if strike_cell.note:
                    lines = [l.strip() for l in strike_cell.note.strip().split('\n') if l.strip()]
                    formatted = []
                    for line in lines:
                        if line.startswith("Strike "):
                            parts = line.split(" - ", 1)
                            formatted.append(f"{parts[0].replace('Strike ', '')} - {parts[1]}" if len(parts) == 2 else line)
                        else:
                            formatted.append(line)
                    if formatted:
                        embed.add_field(name="Strike Reasons", value="\n".join(formatted), inline=False)

                if loa_value != "N/A":
                    loa_cell = await safe_sheets_call(lambda: ws.cell(staff_row_idx, 7))
                    if loa_cell.note:
                        embed.add_field(name="LOA Reason", value=loa_cell.note, inline=False)
        except Exception as e:
            print(f"Error fetching cell notes: {e}")

        embed.set_footer(text=f"Row {staff_row_idx} | {CURRENT_STAFF_SHEET}")
        await interaction.followup.send(embed=embed)

    except gspread.WorksheetNotFound:
        await interaction.followup.send(f"Sheet '{CURRENT_STAFF_SHEET}' not found!")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")
# -------------------------------------------------
#  TIKTOK REMINDER TASK (9 PM UK, every 3 days)
# -------------------------------------------------
TIKTOK_ANNOUNCEMENT_CHANNEL_ID = 1491161732880666818
TIKTOK_START_DATE = dt.date(2026, 4, 27)  # today — the cycle starts here

@tasks.loop(minutes=1)
async def weekly_tiktok_task():
    import datetime as dt
    now_utc = dt.datetime.utcnow()
    month = now_utc.month
    utc_offset = 1 if 4 <= month <= 10 else 0
    now_uk = now_utc + dt.timedelta(hours=utc_offset)

    # Only fire at 21:00 UK time
    if now_uk.strftime("%H:%M") != "21:15":
        return

    # Only fire every 3 days from the start date
    days_since_start = (now_uk.date() - TIKTOK_START_DATE).days
    if days_since_start < 0 or days_since_start % 3 != 0:
        return

    # Deduplicate — prevent double-sends within the same minute
    minute_key = now_uk.strftime("%Y-%m-%d-%H-%M")
    if not hasattr(weekly_tiktok_task, 'sent_keys'):
        weekly_tiktok_task.sent_keys = set()
    if minute_key in weekly_tiktok_task.sent_keys:
        return
    weekly_tiktok_task.sent_keys.add(minute_key)

    # Trim old keys (keep only last 4 days)
    cutoff = (now_uk - dt.timedelta(days=4)).strftime("%Y-%m-%d-%H-%M")
    weekly_tiktok_task.sent_keys = {k for k in weekly_tiktok_task.sent_keys if k > cutoff}

    ch = bot.get_channel(TIKTOK_ANNOUNCEMENT_CHANNEL_ID)
    if not ch:
        print("[TikTok] Channel not found!")
        return

    message = (
        "## <:Winstree_Logo:1484894179552133312> | Winstree x TikTok\n"
        "Hi everyone,\n"
        "We're looking to grow Winstree Academy and bring in more players, and one of the best ways to do that right now is through TikTok.\n\n"
        "We'd like to encourage all staff to start getting involved in making short TikTok videos that show off the game. This can be anything from gameplay clips and roleplay moments to funny scenes, trends, or just general content that represents the academy well.\n\n"
        "The main goal is for each staff member to appear in at least one TikTok per week. You don't have to do this alone either. Teaming up with other staff is completely fine and honestly probably makes it easier and more fun.\n\n"
        "Just keep the content appropriate and in line with the community. Other than that, feel free to be creative and try different ideas.\n\n"
        "This is a simple way for us to get more eyes on the game and grow the community, so any effort here really helps.\n\n"
        "If you're unsure where to start or have ideas, feel free to reach out to the Marketing Team!\n"
        "<@&1484863012933210143>"
    )
    try:
        await ch.send(message)
        print(f"[TikTok] Reminder sent at {now_uk.strftime('%Y-%m-%d %H:%M')} UK time")
    except Exception as e:
        print(f"[TikTok] Failed to send: {e}")

@weekly_tiktok_task.before_loop
async def before_weekly_tiktok():
    await bot.wait_until_ready()
    print("TikTok reminder task started (every 3 days at 21:00 UK)")


# -------------------------------------------------
#  /checkattendance
# -------------------------------------------------
@bot.tree.command(name="checkattendance", description="Check attendance across all current staff")
@cooldown()
async def check_attendance(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        all_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(CURRENT_STAFF_SHEET).get_all_values()
        )

        not_met, exceptional, outstanding = [], [], []

        for row in all_data[CURRENT_STAFF_DATA_START - 1:]:
            if len(row) <= 9:
                continue
            name           = row[3].strip()
            attendance_raw = row[9].strip()
            if not name or not attendance_raw:
                continue
            try:
                attendance = int(float(attendance_raw))
            except ValueError:
                continue
            if attendance == 7:
                outstanding.append(name)
            elif attendance >= 5:
                exceptional.append(name)
            elif attendance < 4:
                not_met.append(name)

        def fmt(names):
            return "\n".join(f"• {n}" for n in names) if names else "*None*"

        embed = discord.Embed(title="📋 Attendance Report", color=discord.Color.blue(), timestamp=datetime.now())
        embed.add_field(name=f"❌ Not Met ({len(not_met)})",        value=fmt(not_met),     inline=False)
        embed.add_field(name=f"⭐ Exceptional ({len(exceptional)})", value=fmt(exceptional), inline=False)
        embed.add_field(name=f"🏆 Outstanding ({len(outstanding)})", value=fmt(outstanding), inline=False)
        embed.set_footer(text=f"{len(not_met)+len(exceptional)+len(outstanding)} staff members checked")
        await interaction.followup.send(embed=embed)

    except gspread.WorksheetNotFound:
        await interaction.followup.send(f"Sheet '{CURRENT_STAFF_SHEET}' not found!")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")


# -------------------------------------------------
#  /viewstaff
# -------------------------------------------------
@bot.tree.command(name="viewstaff", description="View all current staff members and their roles")
@cooldown()
async def view_staff(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        all_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(CURRENT_STAFF_SHEET).get_all_values()
        )

        roles_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(ROLES_LIST_SHEET).get_all_values()
        )

        roles_order = []
        for row in roles_data:
            for cell in row:
                cell = cell.strip()
                if cell and cell not in roles_order:
                    roles_order.append(cell)

        roles_order.reverse()
        if "School Staff" in roles_order:
            roles_order.remove("School Staff")
            roles_order.append("School Staff")

        staff_by_role: dict[str, list[str]] = {role: [] for role in roles_order}
        unmatched: list[tuple[str, str]] = []

        for row in all_data[CURRENT_STAFF_DATA_START - 1:]:
            name = safe_get(row, CURRENT_STAFF_NAME_COL, "").strip()
            role = safe_get(row, 1, "").strip()
            if not name or name == "N/A":
                continue
            if role in staff_by_role:
                staff_by_role[role].append(name)
            else:
                unmatched.append((name, role))

        lines = []
        total = 0
        for role in roles_order:
            members = sorted(staff_by_role.get(role, []))
            if not members:
                continue
            total += len(members)
            lines.append(f"__**{role}**__")
            for name in members:
                lines.append(f"• {name}")
            lines.append("")

        if unmatched:
            lines.append("__**❓ Unknown Role**__")
            for name, role in sorted(unmatched):
                lines.append(f"• {name} — {role}")
            lines.append("")

        full_text = "\n".join(lines).strip()

        chunks = []
        current_chunk = ""
        for line in full_text.split("\n"):
            if len(current_chunk) + len(line) + 1 > 4000:
                chunks.append(current_chunk.strip())
                current_chunk = ""
            current_chunk += line + "\n"
        if current_chunk.strip():
            chunks.append(current_chunk.strip())

        if not chunks:
            await interaction.followup.send("❌ No current staff members found.")
            return

        first_embed = discord.Embed(
            title="👥 Current Staff",
            description=chunks[0],
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        first_embed.set_footer(text=f"{total} staff member(s) total")
        await interaction.followup.send(embed=first_embed)

        for i, chunk in enumerate(chunks[1:], start=2):
            cont_embed = discord.Embed(
                description=chunk,
                color=discord.Color.blue()
            )
            cont_embed.set_footer(text=f"Page {i} • {total} staff member(s) total")
            await interaction.followup.send(embed=cont_embed)

    except gspread.WorksheetNotFound:
        await interaction.followup.send(f"❌ Sheet '{CURRENT_STAFF_SHEET}' not found!")
    except Exception as e:
        await interaction.followup.send(f"❌ Unexpected error: {e}")


# -------------------------------------------------
#  /resetattendance
# -------------------------------------------------
@bot.tree.command(name="resetattendance", description="Reset all attendance records to 0 on Current Staff sheet")
@cooldown()
async def reset_attendance(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        def _reset():
            worksheet  = spreadsheet.worksheet(CURRENT_STAFF_SHEET)
            last_row   = worksheet.row_count
            cell_range = worksheet.range(f"J{CURRENT_STAFF_DATA_START}:J{last_row}")
            for cell in cell_range:
                cell.value = 0
            worksheet.update_cells(cell_range)
            return len(cell_range)

        rows_reset = await safe_sheets_call(_reset)

        embed = discord.Embed(title="Attendance Reset Complete!", color=discord.Color.green())
        embed.add_field(name="Sheet",      value=CURRENT_STAFF_SHEET,           inline=True)
        embed.add_field(name="Column",     value="J (Attendance This Week)",     inline=True)
        embed.add_field(name="Rows Reset", value=f"{rows_reset} rows set to 0", inline=True)
        embed.set_footer(text=f"All attendance in {CURRENT_STAFF_SHEET} has been reset to 0")
        await interaction.followup.send(embed=embed)

    except gspread.WorksheetNotFound:
        await interaction.followup.send(f"Sheet '{CURRENT_STAFF_SHEET}' not found!")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")


# -------------------------------------------------
#  /setstatus
# -------------------------------------------------
@bot.tree.command(name="setstatus", description="Change the bot's status message")
@app_commands.describe(status_text="The new status text", status_type="Type of status")
@app_commands.choices(status_type=[
    app_commands.Choice(name="Playing",      value="playing"),
    app_commands.Choice(name="Watching",     value="watching"),
    app_commands.Choice(name="Listening to", value="listening"),
    app_commands.Choice(name="Competing in", value="competing"),
    app_commands.Choice(name="Custom",       value="custom"),
])
@cooldown()
async def set_status(interaction: discord.Interaction, status_text: str, status_type: str = "custom"):
    await interaction.response.defer(ephemeral=True)
    try:
        if status_type == "playing":
            activity = discord.Game(name=status_text)
        elif status_type == "watching":
            activity = discord.Activity(type=discord.ActivityType.watching, name=status_text)
        elif status_type == "listening":
            activity = discord.Activity(type=discord.ActivityType.listening, name=status_text)
        elif status_type == "competing":
            activity = discord.Activity(type=discord.ActivityType.competing, name=status_text)
        else:
            activity = discord.CustomActivity(name=status_text)

        await bot.change_presence(activity=activity)

        embed = discord.Embed(title="✅ Status Updated!", color=discord.Color.green())
        embed.add_field(name="Type", value=status_type.capitalize(), inline=True)
        embed.add_field(name="Text", value=status_text,              inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to update status: {e}", ephemeral=True)


# -------------------------------------------------
#  /ping  /hello
# -------------------------------------------------
@bot.tree.command(name="ping", description="Check latency")
@cooldown()
async def slash_ping(interaction: discord.Interaction):
    await interaction.response.send_message(f'Pong! Latency: {round(bot.latency * 1000)}ms')

@bot.tree.command(name="hello", description="Say hello to the bot")
@cooldown()
async def slash_hello(interaction: discord.Interaction):
    await interaction.response.send_message('Goodbye')


# -------------------------------------------------
#  /staffblacklist
# -------------------------------------------------
@bot.tree.command(name="staffblacklist", description="[ADMIN] Add a user to the staff blacklist")
@app_commands.describe(
    discord_account="The Discord user to blacklist",
    roblox_username="Their Roblox username",
    reason="Reason for blacklisting",
)
@cooldown()
async def staff_blacklist(
    interaction: discord.Interaction,
    discord_account: discord.Member,
    roblox_username: str,
    reason: str,
):
    await interaction.response.defer()

    discord_id = str(discord_account.id)
    date_str   = datetime.now().strftime("%Y-%m-%d")

    try:
        def _add_to_blacklist():
            ws       = spreadsheet.worksheet(BLACKLISTED_STAFF_SHEET)
            all_data = ws.get_all_values()
            filled   = sum(
                1 for row in all_data[BL_DATA_START - 1:]
                if any(cell.strip() for cell in row[:5])
            )
            next_row = BL_DATA_START + filled
            ws.update(f"B{next_row}:E{next_row}", [[discord_id, roblox_username, date_str, reason]])
            return next_row

        row_num = await safe_sheets_call(_add_to_blacklist)

        embed = discord.Embed(
            title="🚫 Staff Blacklisted",
            color=discord.Color.red(),
            timestamp=datetime.now(),
        )
        embed.add_field(name="Discord User",    value=discord_account.mention, inline=True)
        embed.add_field(name="Discord ID",      value=discord_id,              inline=True)
        embed.add_field(name="Roblox Username", value=roblox_username,         inline=True)
        embed.add_field(name="Date",            value=date_str,                inline=True)
        embed.add_field(name="Reason",          value=reason,                  inline=False)
        embed.set_footer(text=f"Added to row {row_num} of {BLACKLISTED_STAFF_SHEET}")
        await interaction.followup.send(embed=embed)

    except gspread.WorksheetNotFound:
        await interaction.followup.send(f"❌ Sheet '{BLACKLISTED_STAFF_SHEET}' not found!")
    except Exception as e:
        await interaction.followup.send(f"❌ Unexpected error: {e}")


# -------------------------------------------------
#  /unblackliststaff
# -------------------------------------------------
@bot.tree.command(name="unblackliststaff", description="[ADMIN] Remove a user from the staff blacklist")
@app_commands.describe(discord_id="Select the blacklisted staff member")
@app_commands.autocomplete(discord_id=blacklisted_staff_autocomplete)
@cooldown()
async def unblacklist_staff(interaction: discord.Interaction, discord_id: str):
    await interaction.response.defer()

    try:
        def _remove_from_blacklist():
            ws       = spreadsheet.worksheet(BLACKLISTED_STAFF_SHEET)
            all_data = ws.get_all_values()
            for i, row in enumerate(all_data[BL_DATA_START - 1:], start=BL_DATA_START):
                if safe_get(row, BL_DISCORD_ID_COL) == discord_id:
                    ws.delete_rows(i)
                    return True, row
            return False, None

        found, removed_row = await safe_sheets_call(_remove_from_blacklist)

        if found:
            removed_roblox = safe_get(removed_row, BL_ROBLOX_COL) if removed_row else "N/A"
            removed_date   = safe_get(removed_row, BL_DATE_COL)   if removed_row else "N/A"
            removed_reason = safe_get(removed_row, BL_REASON_COL) if removed_row else "N/A"

            embed = discord.Embed(
                title="✅ Staff Unblacklisted",
                color=discord.Color.green(),
                timestamp=datetime.now(),
            )
            embed.add_field(name="Discord ID",              value=discord_id,      inline=True)
            embed.add_field(name="Roblox Username",         value=removed_roblox,  inline=True)
            embed.add_field(name="Original Blacklist Date", value=removed_date,    inline=True)
            embed.add_field(name="Original Reason",         value=removed_reason,  inline=False)
            embed.set_footer(text=f"Removed from {BLACKLISTED_STAFF_SHEET} by {interaction.user.display_name}")
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"❌ Discord ID `{discord_id}` was not found in the blacklist.")

    except gspread.WorksheetNotFound:
        await interaction.followup.send(f"❌ Sheet '{BLACKLISTED_STAFF_SHEET}' not found!")
    except Exception as e:
        await interaction.followup.send(f"❌ Unexpected error: {e}")


# -------------------------------------------------
#  /syncallstaff
# -------------------------------------------------
@bot.tree.command(name="syncallstaff", description="[ADMIN] Sync all staff Discord roles, nicknames and Roblox ranks from the sheet")
@app_commands.describe(dry_run="If True, shows what would change without actually changing anything")
@cooldown()
async def sync_all_staff(interaction: discord.Interaction, dry_run: bool = False):
    await interaction.response.defer(ephemeral=True)

    if not spreadsheet:
        await interaction.followup.send("❌ Google Sheets is not connected.", ephemeral=True)
        return

    guild = interaction.guild
    if not guild:
        await interaction.followup.send("❌ Must be run inside a server.", ephemeral=True)
        return

    try:
        all_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(CURRENT_STAFF_SHEET).get_all_values()
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to read sheet: {e}", ephemeral=True)
        return

    rows = [
        row for row in all_data[CURRENT_STAFF_DATA_START - 1:]
        if len(row) > CURRENT_STAFF_NAME_COL and row[CURRENT_STAFF_NAME_COL].strip()
    ]

    if not rows:
        await interaction.followup.send("❌ No staff found in the sheet.", ephemeral=True)
        return

    total        = len(rows)
    success_disc = 0
    failed_disc  = 0
    success_rbx  = 0
    failed_rbx   = 0
    skipped      = 0
    details      = []

    await interaction.followup.send(
        f"{'🔍 [DRY RUN] ' if dry_run else ''}⏳ Syncing **{total}** staff members... this may take a while.",
        ephemeral=True
    )

    for row in rows:
        teaching_name   = safe_get(row, CURRENT_STAFF_NAME_COL)
        sheet_role      = safe_get(row, 1)
        roblox_username = safe_get(row, 2)
        discord_id_str  = safe_get(row, 7)

        if sheet_role == "N/A" or not sheet_role:
            details.append(f"⚠️ **{teaching_name}** — skipped (no role in sheet)")
            skipped += 1
            continue

        line_parts = [f"**{teaching_name}** ({sheet_role})"]

        if discord_id_str and discord_id_str != "N/A":
            member = await get_discord_member_by_id(guild, discord_id_str)
            if member:
                if not dry_run:
                    try:
                        desired_ids = get_discord_roles_for_sheet_role(sheet_role)
                        desired_role_objects = [
                            guild.get_role(rid) for rid in desired_ids
                            if guild.get_role(rid) is not None
                        ]
                        roles_to_remove = [r for r in member.roles if r.id in ALL_MANAGED_ROLE_IDS]
                        if roles_to_remove:
                            await member.remove_roles(*roles_to_remove, reason="syncallstaff")
                        if desired_role_objects:
                            await member.add_roles(*desired_role_objects, reason="syncallstaff")
                        new_nick = get_nickname_for_sheet_role(teaching_name, sheet_role)[:32]
                        await member.edit(nick=new_nick, reason="syncallstaff")
                        line_parts.append("✅ Discord")
                        success_disc += 1
                    except discord.Forbidden:
                        line_parts.append("⚠️ Discord (no permission)")
                        failed_disc += 1
                    except Exception as e:
                        line_parts.append(f"⚠️ Discord ({e})")
                        failed_disc += 1
                else:
                    expected_nick = get_nickname_for_sheet_role(teaching_name, sheet_role)[:32]
                    current_nick  = member.nick or member.name
                    nick_change   = f"`{current_nick}` → `{expected_nick}`" if current_nick != expected_nick else "nick unchanged"
                    line_parts.append(f"🔍 Discord ({nick_change})")
                    success_disc += 1
            else:
                line_parts.append("⚠️ Discord (not in server)")
                failed_disc += 1
        else:
            line_parts.append("➖ Discord (no ID)")
            skipped += 1

        if roblox_username and roblox_username != "N/A":
            if sheet_role in ROLE_NAME_MAP:
                if not dry_run:
                    success, result = await roblox_set_rank_by_sheet_role(roblox_username, sheet_role)
                    if success:
                        line_parts.append(f"✅ Roblox → {result}")
                        success_rbx += 1
                    else:
                        line_parts.append(f"⚠️ Roblox ({result})")
                        failed_rbx += 1
                else:
                    expected_rank = ROLE_NAME_MAP.get(sheet_role, "unknown")
                    line_parts.append(f"🔍 Roblox → {expected_rank}")
                    success_rbx += 1
            else:
                line_parts.append("➖ Roblox (role not in map)")
        else:
            line_parts.append("➖ Roblox (no username)")

        details.append(" | ".join(line_parts))
        await asyncio.sleep(0.5)

    mode_label = "DRY RUN PREVIEW" if dry_run else "Sync Complete"
    embed = discord.Embed(
        title=f"{'🔍 ' if dry_run else '✅ '}Staff Sync — {mode_label}",
        color=discord.Color.blue() if dry_run else discord.Color.green(),
        timestamp=datetime.now()
    )
    embed.add_field(name="Total Staff", value=total,        inline=True)
    embed.add_field(name="Discord ✅",  value=success_disc, inline=True)
    embed.add_field(name="Discord ⚠️",  value=failed_disc,  inline=True)
    embed.add_field(name="Roblox ✅",   value=success_rbx,  inline=True)
    embed.add_field(name="Roblox ⚠️",   value=failed_rbx,   inline=True)
    embed.add_field(name="Skipped",     value=skipped,      inline=True)

    if dry_run:
        embed.set_footer(text="Dry run — no changes were made. Run /syncallstaff dry_run:False to apply.")
    else:
        embed.set_footer(text=f"Triggered by {interaction.user.display_name}")

    await interaction.followup.send(embed=embed, ephemeral=True)

    chunk = ""
    for line in details:
        if len(chunk) + len(line) + 1 > 1900:
            await interaction.followup.send(f"```\n{chunk.strip()}\n```", ephemeral=True)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await interaction.followup.send(f"```\n{chunk.strip()}\n```", ephemeral=True)


# -------------------------------------------------
#  /syncstaff  — sync a single staff member
# -------------------------------------------------
@bot.tree.command(name="syncstaff", description="Sync a single staff member's Discord roles, nickname and Roblox rank")
@app_commands.describe(staff_name="Select the staff member to sync")
@app_commands.autocomplete(staff_name=staff_autocomplete)
@cooldown()
async def sync_staff(interaction: discord.Interaction, staff_name: str):
    await interaction.response.defer(ephemeral=True)

    if not spreadsheet:
        await interaction.followup.send("❌ Google Sheets is not connected.", ephemeral=True)
        return

    guild = interaction.guild
    if not guild:
        await interaction.followup.send("❌ Must be run inside a server.", ephemeral=True)
        return

    try:
        all_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(CURRENT_STAFF_SHEET).get_all_values()
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to read sheet: {e}", ephemeral=True)
        return

    target_row = None
    for row in all_data[CURRENT_STAFF_DATA_START - 1:]:
        if (
            len(row) > CURRENT_STAFF_NAME_COL
            and row[CURRENT_STAFF_NAME_COL].strip().lower() == staff_name.lower()
        ):
            target_row = row
            break

    if target_row is None:
        await interaction.followup.send(
            f"❌ Staff member **{staff_name}** not found in {CURRENT_STAFF_SHEET}.",
            ephemeral=True
        )
        return

    teaching_name   = safe_get(target_row, CURRENT_STAFF_NAME_COL)
    sheet_role      = safe_get(target_row, 1)
    roblox_username = safe_get(target_row, 2)
    discord_id_str  = safe_get(target_row, 7)

    embed = discord.Embed(
        title=f"🔄 Syncing: {teaching_name}",
        color=discord.Color.blue(),
        timestamp=datetime.now(),
    )
    embed.add_field(name="Role",            value=sheet_role,      inline=True)
    embed.add_field(name="Roblox Username", value=roblox_username, inline=True)
    embed.add_field(name="Discord ID",      value=discord_id_str,  inline=True)

    if discord_id_str and discord_id_str != "N/A":
        member = await get_discord_member_by_id(guild, discord_id_str)
        if member:
            if sheet_role and sheet_role != "N/A":
                await apply_discord_roles_and_nick(
                    guild, member, sheet_role, teaching_name, embed=embed
                )
            else:
                embed.add_field(name="Discord", value="⚠️ No role in sheet — skipped", inline=False)
        else:
            embed.add_field(name="Discord", value="⚠️ Member not found in this server", inline=False)
    else:
        embed.add_field(name="Discord", value="➖ No Discord ID on file", inline=False)

    if roblox_username and roblox_username != "N/A":
        if sheet_role in ROLE_NAME_MAP:
            success, result = await roblox_set_rank_by_sheet_role(roblox_username, sheet_role)
            if success:
                embed.add_field(name="Roblox", value=f"✅ Ranked to **{result}**", inline=False)
            else:
                embed.add_field(name="Roblox", value=f"⚠️ {result}", inline=False)
        else:
            embed.add_field(name="Roblox", value="➖ Role not in Roblox rank map", inline=False)
    else:
        embed.add_field(name="Roblox", value="➖ No Roblox username on file", inline=False)

    embed.set_footer(text=f"Synced by {interaction.user.display_name}")
    await interaction.followup.send(embed=embed, ephemeral=True)


# -------------------------------------------------
#  /viewremovedstaff
# -------------------------------------------------
async def removed_staff_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    try:
        all_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(REMOVE_STAFF_LOG_SHEET).get_all_values()
        )
        names = []
        for row in all_data[RSL_DATA_START - 1:]:
            name = safe_get(row, RSL_NAME_COL)
            if name and name != "N/A" and name not in names:
                names.append(name)

        filtered = [n for n in names if current.lower() in n.lower()]
        return [app_commands.Choice(name=n, value=n) for n in filtered[:25]]
    except Exception as e:
        print(f"[viewremovedstaff] Autocomplete error: {e}")
        return []


@bot.tree.command(name="viewremovedstaff", description="View a removed staff member's departure record")
@app_commands.describe(staff_name="Select the removed staff member")
@app_commands.autocomplete(staff_name=removed_staff_autocomplete)
@cooldown()
async def view_removed_staff(interaction: discord.Interaction, staff_name: str):
    await interaction.response.defer(ephemeral=True)

    try:
        all_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(REMOVE_STAFF_LOG_SHEET).get_all_values()
        )

        records = []
        for row in all_data[RSL_DATA_START - 1:]:
            name = safe_get(row, RSL_NAME_COL)
            if name.strip().lower() == staff_name.strip().lower():
                date       = safe_get(row, RSL_DATE_COL)
                discord_id = safe_get(row, RSL_DISCORD_ID_COL)
                reason     = safe_get(row, RSL_REASON_COL)
                records.append((name, date, discord_id, reason))

        if not records:
            await interaction.followup.send(
                f"❌ No removal record found for **{staff_name}**.",
                ephemeral=False
            )
            return

        embed = discord.Embed(
            title=f"🗂️ Removal Record — {staff_name}",
            color=discord.Color.red(),
            timestamp=datetime.now(),
        )

        for i, (name, date, discord_id, reason) in enumerate(records, start=1):
            label = f"Entry {i}" if len(records) > 1 else "Details"
            embed.add_field(name=f"{label} — Teaching Name",   value=name,   inline=True)
            embed.add_field(name=f"{label} — Date of Removal", value=date,   inline=True)
            embed.add_field(
                name=f"{label} — Discord ID",
                value=f"<@{discord_id}>" if discord_id != "N/A" else "N/A",
                inline=True
            )
            embed.add_field(name=f"{label} — Reason", value=reason, inline=False)

            if i < len(records):
                embed.add_field(name="\u200b", value="─" * 30, inline=False)

        embed.set_footer(text=f"Remove Staff Log · {len(records)} record(s) found")
        await interaction.followup.send(embed=embed, ephemeral=False)

    except gspread.WorksheetNotFound:
        await interaction.followup.send(f"❌ '{REMOVE_STAFF_LOG_SHEET}' sheet not found.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Unexpected error: {e}", ephemeral=True)


# -------------------------------------------------
#  /diagnoseroblox
# -------------------------------------------------
@bot.tree.command(name="diagnoseroblox", description="[ADMIN] Run Roblox authentication diagnostics")
@cooldown()
async def diagnose_roblox(interaction: discord.Interaction):
    await interaction.response.send_message("🔍 Checking environment...", ephemeral=True)

    output = ["=" * 50, "ROBLOX DIAGNOSTICS", "=" * 50]

    output.append("\n[1] Environment variable check...")
    roblox_auth_token = os.getenv('ROBLOX_AUTH_TOKEN')
    output.append(f"ROBLOX_AUTH_TOKEN present: {roblox_auth_token is not None}")
    if roblox_auth_token:
        output.append(f"  Length: {len(roblox_auth_token)} chars")
        output.append(f"  Starts with _|WARNING: {roblox_auth_token.startswith('_|WARNING')}")
        output.append(f"  First 60: {roblox_auth_token[:60]}")
    else:
        output.append("  ⚠️ NOT SET — ranking will fail!")

    output.append("\n[2] Bot memory check...")
    output.append(f"ROBLOX_COOKIE in memory: {ROBLOX_COOKIE is not None}")
    if ROBLOX_COOKIE:
        output.append(f"  Length: {len(ROBLOX_COOKIE)} chars")

    output.append(f"\n[3] Group config...")
    output.append(f"  ROBLOX_GROUP_ID: {ROBLOX_GROUP_ID}")

    output.append("\n[4] Live group role fetch test...")
    try:
        api = get_roblox_api()
        if api:
            loop  = asyncio.get_running_loop()
            roles = await loop.run_in_executor(None, api.get_group_roles)
            if roles:
                output.append(f"  ✅ Fetched {len(roles)} roles from the group:")
                for r in sorted(roles, key=lambda x: x["rank"]):
                    output.append(f"    Rank {r['rank']:>3}: {r['name']} (id={r['id']})")
            else:
                output.append("  ⚠️ No roles returned — check group ID")
        else:
            output.append("  ⚠️ No cookie — skipped")
    except Exception as e:
        output.append(f"  ❌ Error: {e}")

    output.append("\n[5] ROLE_NAME_MAP entries...")
    for sheet_role, roblox_rank in ROLE_NAME_MAP.items():
        output.append(f"  {sheet_role!r} → {roblox_rank!r}")

    output.append("\n[6] Discord role ID config...")
    output.append("  Rank roles:")
    for name, rid in DISCORD_RANK_ROLE_IDS.items():
        output.append(f"    {name}: {rid}")
    output.append("  Extra roles:")
    for name, rid in DISCORD_EXTRA_ROLE_IDS.items():
        output.append(f"    {name}: {rid}")

    output.append("\n[7] Name initialling test...")
    test_cases = [
        ("Miss Zoe Parker",       "Head of Year 7"),
        ("Miss Zoe Parker",       "Assistant Headteacher"),
        ("Dr James Andrew Smith", "School Staff"),
        ("Mr John Williams",      "Deputy Head of Year 9"),
        ("Mrs Sarah Thompson",    "Head of Lower Level"),
        ("Prof Elizabeth Brown",  "Deputy Headteacher"),
    ]
    for name, role in test_cases:
        result = get_nickname_for_sheet_role(name, role)
        output.append(f"  {name!r} + {role!r} → {result!r}")

    output.append("\n" + "=" * 50)
    full_output = "\n".join(output)

    chunks = [full_output[i:i+1900] for i in range(0, len(full_output), 1900)]
    await interaction.edit_original_response(content=f"```\n{chunks[0]}\n```")
    for chunk in chunks[1:]:
        await interaction.followup.send(f"```\n{chunk}\n```", ephemeral=True)


# -------------------------------------------------
#  STUDENT NAME REQUEST SYSTEM
# -------------------------------------------------

STUDENT_NAME_LOG_ID = 1489698033573826601

class StudentNameRequestView(discord.ui.View):
    def __init__(self, requester_id: int, requested_name: str):
        super().__init__(timeout=None)
        self.requester_id   = requester_id
        self.requested_name = requested_name

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅", custom_id="stud_name_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        guild     = interaction.guild
        member    = None
        error_log = ""

        try:
            member = await guild.fetch_member(self.requester_id)
        except Exception as e:
            error_log = f"Could not find member: {e}"

        if member:
            try:
                await member.edit(nick=self.requested_name)
            except discord.Forbidden:
                error_log += " | Bot lacks 'Manage Nicknames' or Hierarchy is too low."
            except Exception as e:
                error_log += f" | Nick Error: {e}"

            try:
                dm_embed = discord.Embed(
                    title="Name Request Approved",
                    description=f"Your request to change your display name to **{self.requested_name}** has been approved.\n\nYour Discord nickname has been updated accordingly. If you notice any discrepancies, please contact a member of the Senior Leadership Team.",
                    color=discord.Color.green()
                )
                await member.send(embed=dm_embed)
            except Exception as e:
                error_log += f" | DM Error: {e} (User likely has DMs off)"

        for child in self.children:
            child.disabled = True

        new_embed = interaction.message.embeds[0]
        new_embed.title = "✅ Approved & Processed"
        if error_log:
            new_embed.add_field(name="System Logs", value=f"```{error_log}```", inline=False)

        await interaction.message.edit(embed=new_embed, view=self)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌", custom_id="stud_name_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        guild     = interaction.guild
        member    = None
        dm_status = "✅ Sent"

        try:
            member = await guild.fetch_member(self.requester_id)
            dm_embed = discord.Embed(
                title="Name Request Unsuccessful",
                description=f"Thank you for submitting your name change request to **{self.requested_name}**.\n\nUnfortunately, your request has not been approved at this time. If you believe this is an error or would like further clarification, please reach out to a member of the Senior Leadership Team.",
                color=discord.Color.red()
            )
            await member.send(embed=dm_embed)
        except Exception as e:
            dm_status = f"❌ Failed: {e}"

        for child in self.children:
            child.disabled = True

        new_embed = interaction.message.embeds[0]
        new_embed.title = "❌ Request Denied"
        new_embed.add_field(name="DM Status", value=dm_status)

        await interaction.message.edit(embed=new_embed, view=self)

@bot.tree.command(name="requestdisplayname", description="Request a change to your student display name")
@app_commands.describe(new_name="The name you would like to be displayed as")
async def request_display_name(interaction: discord.Interaction, new_name: str):
    if len(new_name) > 32:
        return await interaction.response.send_message("❌ Names must be under 32 characters.", ephemeral=True)

    log_channel = bot.get_channel(STUDENT_NAME_LOG_ID)
    if not log_channel:
        return await interaction.response.send_message("❌ Log channel not found.", ephemeral=True)

    request_embed = discord.Embed(
        title="🎓 Student Name Change Request",
        description="A student has requested a name change.",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    request_embed.add_field(name="Student",          value=interaction.user.mention,      inline=True)
    request_embed.add_field(name="Current Nickname", value=interaction.user.display_name, inline=True)
    request_embed.add_field(name="Requested Name",   value=f"`{new_name}`",              inline=False)
    request_embed.set_footer(text=f"User ID: {interaction.user.id}")

    view = StudentNameRequestView(
        requester_id=interaction.user.id,
        requested_name=new_name
    )

    await log_channel.send(embed=request_embed, view=view)
    await interaction.response.send_message("✅ Your request has been sent to the Senior Leadership Team.", ephemeral=True)


# -------------------------------------------------
#  /customrolerequest
# -------------------------------------------------

ROLE_REQUEST_CHANNEL_ID = 1489704193467088997

def is_valid_hex(hex_code: str):
    if not hex_code:
        return False
    return bool(re.search(r'^#(?:[0-9a-fA-F]{3}){1,2}$|^[0-9a-fA-F]{6}$', hex_code))

class CustomRoleView(discord.ui.View):
    def __init__(self, requester_id: int, role_name: str, hex_color: str):
        super().__init__(timeout=None)
        self.requester_id = requester_id
        self.role_name    = role_name
        self.hex_color    = hex_color

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅", custom_id="role_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        guild  = interaction.guild
        member = await guild.fetch_member(self.requester_id)

        if not member:
            return await interaction.followup.send("Member not found in server.", ephemeral=True)

        clean_hex = self.hex_color.replace("#", "") if self.hex_color else ""
        if is_valid_hex(clean_hex):
            color_value = discord.Color(int(clean_hex, 16))
        else:
            color_value = discord.Color.default()

        try:
            new_role = await guild.create_role(
                name=self.role_name,
                color=color_value,
                reason=f"Custom role approved for {member.display_name} by {interaction.user.display_name}"
            )

            await member.add_roles(new_role)

            dm_embed = discord.Embed(
                title="Custom Role Approved!",
                description=(
                    f"Your request for the custom role **{self.role_name}** has been approved.\n\n"
                    f"The role has been created and added to your profile. Enjoy your new look!"
                ),
                color=color_value
            )
            await member.send(embed=dm_embed)

            for child in self.children:
                child.disabled = True
            log_embed = interaction.message.embeds[0]
            log_embed.title = "✅ Role Request Approved & Created"
            log_embed.color = discord.Color.green()
            await interaction.message.edit(embed=log_embed, view=self)

        except discord.Forbidden:
            await interaction.followup.send("❌ Bot lacks 'Manage Roles' permission.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌", custom_id="role_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        guild  = interaction.guild
        member = await guild.fetch_member(self.requester_id)

        if member:
            try:
                dm_embed = discord.Embed(
                    title="Role Request Denied",
                    description=(
                        f"Unfortunately, your request for the custom role **{self.role_name}** was not approved at this time.\n\n"
                        f"If you have questions regarding this decision, please contact the Senior Leadership Team."
                    ),
                    color=discord.Color.red()
                )
                await member.send(embed=dm_embed)
            except Exception:
                pass

        for child in self.children:
            child.disabled = True
        log_embed = interaction.message.embeds[0]
        log_embed.title = "❌ Role Request Denied"
        log_embed.color = discord.Color.red()
        await interaction.message.edit(embed=log_embed, view=self)

@bot.tree.command(name="customrolerequest", description="Request a custom role with a specific name and color")
@app_commands.describe(name="The name of the role", color="The Hex color code (e.g. #ff0000 or ff0000)")
async def customrolerequest(interaction: discord.Interaction, name: str, color: str = None):
    log_channel = bot.get_channel(ROLE_REQUEST_CHANNEL_ID)
    if not log_channel:
        return await interaction.response.send_message("Log channel not found.", ephemeral=True)

    preview_color = discord.Color.blue()
    clean_hex     = color.replace("#", "") if color else ""
    if is_valid_hex(clean_hex):
        preview_color = discord.Color(int(clean_hex, 16))

    request_embed = discord.Embed(
        title="🎨 New Custom Role Request",
        color=preview_color,
        timestamp=interaction.created_at
    )
    request_embed.add_field(name="User",            value=interaction.user.mention,   inline=True)
    request_embed.add_field(name="Requested Name",  value=name,                       inline=True)
    request_embed.add_field(name="Requested Color", value=f"`{color or 'Default'}`", inline=True)
    request_embed.set_footer(text=f"User ID: {interaction.user.id}")

    view = CustomRoleView(requester_id=interaction.user.id, role_name=name, hex_color=color)

    await log_channel.send(embed=request_embed, view=view)
    await interaction.response.send_message("✅ Your role request has been submitted!", ephemeral=True)

# ---- SNIPPET TO INSERT INTO bot.py ----
# Place COMMAND_LOG_CHANNEL_ID right after BLOCKED_APP_LOG_CHANNEL_ID:
#
#   BLOCKED_APP_LOG_CHANNEL_ID = 1495061332741853438
#   COMMAND_LOG_CHANNEL_ID     = 1495796902082646209  ← ADD THIS
#
# Then place the on_interaction event handler shown below
# directly after the on_ready event (or anywhere in the bot events section).


@bot.event
async def on_interaction(interaction: discord.Interaction):
    # Only log slash / context-menu commands (type 2), not buttons / modals
    if interaction.type != discord.InteractionType.application_command:
        return

    log_channel = bot.get_channel(COMMAND_LOG_CHANNEL_ID)
    if not log_channel:
        return

    try:
        command_name = (
            interaction.command.qualified_name
            if interaction.command
            else interaction.data.get("name", "Unknown")
        )

        # ---------- format options / parameters ----------
        def _fmt_options(options: list, depth: int = 0) -> list[str]:
            lines = []
            indent = "  " * depth
            for opt in options:
                name  = opt.get("name", "?")
                value = opt.get("value")
                sub   = opt.get("options", [])
                if value is not None:
                    lines.append(f"{indent}`{name}`: {value}")
                elif sub:
                    lines.append(f"{indent}`{name}`:")
                    lines.extend(_fmt_options(sub, depth + 1))
                else:
                    lines.append(f"{indent}`{name}`")
            return lines

        raw_options  = interaction.data.get("options", [])
        params_lines = _fmt_options(raw_options)
        params_text  = "\n".join(params_lines) if params_lines else "*none*"

        # ---------- channel / guild info ----------
        if interaction.channel:
            if hasattr(interaction.channel, "mention"):
                channel_text = f"{interaction.channel.mention}\n`#{interaction.channel.name}`"
            else:
                channel_text = f"`{interaction.channel}`"
        else:
            channel_text = "Unknown / DM"

        guild_text = (
            f"{interaction.guild.name}\n`{interaction.guild.id}`"
            if interaction.guild
            else "DM"
        )

        # ---------- build embed ----------
        embed = discord.Embed(
            title="🖥️ Slash Command Used",
            color=discord.Color.blurple(),
            timestamp=datetime.now(),
        )
        embed.add_field(
            name="Command",
            value=f"`/{command_name}`",
            inline=True,
        )
        embed.add_field(
            name="User",
            value=(
                f"{interaction.user.mention}\n"
                f"`{interaction.user}` — ID: `{interaction.user.id}`"
            ),
            inline=True,
        )
        embed.add_field(
            name="Channel",
            value=channel_text,
            inline=True,
        )
        embed.add_field(
            name="Server",
            value=guild_text,
            inline=True,
        )
        embed.add_field(
            name="Time (UTC)",
            value=f"<t:{int(interaction.created_at.timestamp())}:F>\n<t:{int(interaction.created_at.timestamp())}:R>",
            inline=True,
        )
        embed.add_field(
            name="Parameters",
            value=params_text[:1024],  # embed field limit
            inline=False,
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(
            text=(
                f"Interaction ID: {interaction.id}  •  "
                f"Guild: {interaction.guild_id}  •  "
                f"User: {interaction.user.id}"
            )
        )

        await log_channel.send(embed=embed)

    except Exception as e:
        print(f"[CommandLog] Failed to log interaction: {e}")


# -------------------------------------------------
#  /apply
# -------------------------------------------------

APPLICATION_LOG_CHANNEL_ID = 1489748153480773722

APPLICATION_BLOCKED_DM = "Oops! You can't apply for that position right now. Try again later!"

WELCOME_DM = (
    "## ❗ | Congratulations on Your Appointment at Winstree Academy\n"
    "We are pleased to welcome you to the staff team at Winstree Academy.\n\n"
    "***__Important Information__***\n"
    "- You are required to complete your Initial Teacher Training within one week of receiving this message.\n"
    "- All staff members are expected to attend four sessions per week (Sunday–Saturday).\n"
    "- High standards of grammar, punctuation, and spelling (SPaG) must be maintained at all times while on school grounds.\n"
    "- Staff sessions begin at 19:45 (UK time) and conclude at 21:10.\n\n"
    "Your teaching name and assigned roles have already been recorded. Please run /profile in the server to review your details.\n\n"
    "We look forward to your attendance at today's session, commencing at 19:45 BST in the briefing room.\n\n"
    "*Senior Leadership Team*\n"
    "**Winstree Academy**"
)

REJECTION_DM = (
    "## ❌ | Application Unsuccessful — Winstree Academy\n"
    "Thank you for taking the time to apply for a staff position at Winstree Academy.\n\n"
    "After careful consideration, we regret to inform you that your application has been unsuccessful at this time.\n\n"
    "You are welcome to reapply in the future. If you have any questions, please do not hesitate to reach out to a member of the Senior Leadership Team.\n\n"
    "*Senior Leadership Team*\n"
    "**Winstree Academy**"
)


async def _send_blocked_notification(
    client: discord.Client,
    user: discord.User | discord.Member,
    discord_id: str,
    block_reason: str,
    roblox_username: str = "N/A",
    teaching_name: str = "N/A",
    stage: str = "/apply command",
) -> None:
    """Shared helper — posts a blocked-application embed to BLOCKED_APP_LOG_CHANNEL_ID."""
    blocked_channel = client.get_channel(BLOCKED_APP_LOG_CHANNEL_ID)
    if not blocked_channel:
        return

    if block_reason == "blacklisted":
        reason_label = "🚫 User is on the staff blacklist"
        embed_color  = discord.Color.red()
    elif block_reason == "recent_staff":
        reason_label = "⏳ User was removed from staff within the last 2 weeks"
        embed_color  = discord.Color.orange()
    else:
        reason_label = f"Unknown (`{block_reason}`)"
        embed_color  = discord.Color.greyple()

    blocked_embed = discord.Embed(
        title="⛔ Blocked Application Attempt",
        description=f"A user was automatically blocked at the **{stage}** stage.",
        color=embed_color,
        timestamp=datetime.now(),
    )
    blocked_embed.add_field(name="User",            value=user.mention,     inline=True)
    blocked_embed.add_field(name="Discord ID",      value=discord_id,       inline=True)
    blocked_embed.add_field(name="Roblox Username", value=roblox_username,  inline=True)
    blocked_embed.add_field(name="Teaching Name",   value=teaching_name,    inline=True)
    blocked_embed.add_field(name="Reason Blocked",  value=reason_label,     inline=False)
    blocked_embed.set_footer(text=f"User ID: {discord_id}")

    try:
        await blocked_channel.send(embed=blocked_embed)
    except Exception as e:
        print(f"[Apply] Failed to send blocked notification: {e}")


class ApplicationModal(discord.ui.Modal, title="Winstree Academy - Teaching Staff Application"):

    roblox_username = discord.ui.TextInput(
        label="Roblox Username",
        placeholder="Enter your exact Roblox username",
        required=True,
        max_length=50,
    )

    teaching_name = discord.ui.TextInput(
        label="Full teaching name - initials will be denied.",
        placeholder="e.g. Miss Zoe Parker",
        required=True,
        max_length=60,
    )

    reason = discord.ui.TextInput(
        label="Why do you want to be staff?",
        placeholder="Write a couple of sentences about why you'd like to join the team...",
        style=discord.TextStyle.paragraph,
        required=True,
        min_length=30,
        max_length=500,
    )

    def __init__(self, age_range: str):
        super().__init__()
        self.age_range = age_range

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        discord_id = str(interaction.user.id)

        # Safety-net check — catches anyone who somehow bypassed the /apply command check
        can_apply, block_reason = await check_application_eligibility(discord_id)

        if not can_apply:
            try:
                await interaction.user.send(APPLICATION_BLOCKED_DM)
            except Exception:
                pass

            await interaction.followup.send(
                "Oops! You can't apply for that position right now. Try again later!",
                ephemeral=True,
            )

            print(f"[Apply] Blocked {interaction.user} (ID: {discord_id}) at modal stage — reason: {block_reason}")

            await _send_blocked_notification(
                client=interaction.client,
                user=interaction.user,
                discord_id=discord_id,
                block_reason=block_reason,
                roblox_username=self.roblox_username.value,
                teaching_name=self.teaching_name.value,
                stage="modal submission",
            )
            return

        log_channel = interaction.client.get_channel(APPLICATION_LOG_CHANNEL_ID)
        if not log_channel:
            await interaction.followup.send(
                "❌ Could not find the application log channel. Please contact an administrator.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="📋 New Staff Application",
            color=discord.Color.orange(),
            timestamp=datetime.now()
        )
        embed.add_field(name="Applicant",             value=interaction.user.mention,   inline=True)
        embed.add_field(name="Discord ID",            value=str(interaction.user.id),   inline=True)
        embed.add_field(name="Roblox Username",       value=self.roblox_username.value, inline=True)
        embed.add_field(name="Desired Teaching Name", value=self.teaching_name.value,   inline=True)
        embed.add_field(name="Age Range",             value=self.age_range,             inline=True)
        embed.add_field(
            name="Why do you want to be staff?",
            value=self.reason.value,
            inline=False
        )
        embed.set_footer(text=f"User ID: {interaction.user.id}")

        view = ApplicationReviewView(
            applicant_id=interaction.user.id,
            roblox_username=self.roblox_username.value,
            teaching_name=self.teaching_name.value,
            age_range=self.age_range,
            reason=self.reason.value,
        )

        await log_channel.send(content="@here", embed=embed, view=view)
        await interaction.followup.send(
            "✅ Your application has been submitted! You will receive a DM once it has been reviewed by the Senior Leadership Team.",
            ephemeral=True
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[Apply] Modal error: {error}")
        try:
            await interaction.response.send_message(
                "❌ Something went wrong submitting your application. Please try again.",
                ephemeral=True
            )
        except Exception:
            pass


class AgeRangeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Under 9", value="U9",    emoji="🔞"),
            discord.SelectOption(label="9–12",    value="9-12",  emoji="🎒"),
            discord.SelectOption(label="13–15",   value="13-15", emoji="📚"),
            discord.SelectOption(label="16–17",   value="16-17", emoji="🎓"),
            discord.SelectOption(label="18–20",   value="18-20", emoji="🏫"),
            discord.SelectOption(label="21+",     value="21+",   emoji="👤"),
        ]
        super().__init__(
            placeholder="Select your age range to begin...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="age_range_select",
        )

    async def callback(self, interaction: discord.Interaction):
        age_range = self.values[0]

        if age_range == "U9":
            await interaction.response.send_message(
                "❌ You must be aged 9 or over to apply for a staff position at Winstree Academy.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(ApplicationModal(age_range=age_range))


class AgeRangeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(AgeRangeSelect())

    async def on_timeout(self):
        pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"[Apply] AgeRangeView error: {error}")
        try:
            await interaction.response.send_message(
                "❌ Something went wrong. Please try running /apply again.",
                ephemeral=True
            )
        except Exception:
            pass


class ApplicationReviewView(discord.ui.View):
    def __init__(
        self,
        applicant_id: int,
        roblox_username: str,
        teaching_name: str,
        age_range: str,
        reason: str,
    ):
        super().__init__(timeout=None)
        self.applicant_id    = applicant_id
        self.roblox_username = roblox_username
        self.teaching_name   = teaching_name
        self.age_range       = age_range
        self.reason          = reason

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅", custom_id="app_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        guild  = interaction.guild
        member = None
        try:
            member = await guild.fetch_member(self.applicant_id)
        except Exception:
            pass

        if self.age_range in ("9-12", "13-15", "16-17"):
            area = "Academy"
        else:
            area = "Sixth Form"

        role = "School Staff"

        sheet_status = ""
        params = {
            "action":        "hire",
            "teachingName":  self.teaching_name,
            "staffUsername": self.roblox_username,
            "area":          area,
            "position":      role,
            "discordId":     str(self.applicant_id),
        }
        try:
            status, response_text = await safe_apps_script_get(APPS_SCRIPT_URL, params)
            if status == 200 and "error" not in response_text.lower():
                sheet_status = "✅ Added to sheet"
            else:
                sheet_status = f"⚠️ Sheet error (HTTP {status}): {response_text[:200]}"
        except Exception as e:
            sheet_status = f"⚠️ Sheet exception: {e}"

        roblox_status = ""
        try:
            success, result = await roblox_set_rank_by_sheet_role(self.roblox_username, role)
            if success:
                roblox_status = f"✅ Ranked to **{result}**"
            else:
                roblox_status = f"⚠️ {result}"
        except Exception as e:
            roblox_status = f"⚠️ Roblox exception: {e}"

        discord_status = ""
        if guild and member:
            try:
                desired_ids = get_discord_roles_for_sheet_role(role)
                desired_role_objects = [
                    guild.get_role(rid) for rid in desired_ids
                    if guild.get_role(rid) is not None
                ]
                roles_to_remove = [r for r in member.roles if r.id in ALL_MANAGED_ROLE_IDS]
                if roles_to_remove:
                    await member.remove_roles(*roles_to_remove, reason="Application accepted")
                if desired_role_objects:
                    await member.add_roles(*desired_role_objects, reason=f"Application accepted — {role}")
                new_nick = get_nickname_for_sheet_role(self.teaching_name, role)[:32]
                await member.edit(nick=new_nick, reason="Application accepted")
                role_names     = ", ".join(r.name for r in desired_role_objects) or "None"
                discord_status = f"✅ Roles: {role_names}\n✅ Nickname: `{new_nick}`"
            except discord.Forbidden:
                discord_status = "⚠️ Missing permissions to manage roles or nickname"
            except Exception as e:
                discord_status = f"⚠️ Discord error: {e}"
        else:
            discord_status = "⚠️ Member not found in server"

        dm_status = ""
        if member:
            try:
                await member.send(WELCOME_DM)
                dm_status = "✅ Welcome DM sent"
            except discord.Forbidden:
                dm_status = "⚠️ Could not DM (user may have DMs disabled)"
            except Exception as e:
                dm_status = f"⚠️ DM error: {e}"
        else:
            dm_status = "⚠️ Member not found in server"

        for child in self.children:
            child.disabled = True

        original_embed = interaction.message.embeds[0]
        original_embed.title = "✅ Application Accepted"
        original_embed.color = discord.Color.green()
        original_embed.add_field(name="Area Assigned", value=area,                     inline=True)
        original_embed.add_field(name="Sheet",         value=sheet_status,             inline=False)
        original_embed.add_field(name="Roblox",        value=roblox_status,            inline=False)
        original_embed.add_field(name="Discord",       value=discord_status,           inline=False)
        original_embed.add_field(name="Welcome DM",    value=dm_status,                inline=False)
        original_embed.add_field(name="Reviewed By",   value=interaction.user.mention, inline=False)

        await interaction.message.edit(embed=original_embed, view=self)
        refresh_staff_names_cache()

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌", custom_id="app_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        guild  = interaction.guild
        member = None
        try:
            member = await guild.fetch_member(self.applicant_id)
        except Exception:
            pass

        dm_status = ""
        if member:
            try:
                await member.send(REJECTION_DM)
                dm_status = "✅ Rejection DM sent"
            except discord.Forbidden:
                dm_status = "⚠️ Could not DM (user may have DMs disabled)"
            except Exception as e:
                dm_status = f"⚠️ DM error: {e}"
        else:
            dm_status = "⚠️ Member not found in server"

        for child in self.children:
            child.disabled = True

        original_embed = interaction.message.embeds[0]
        original_embed.title = "❌ Application Denied"
        original_embed.color = discord.Color.red()
        original_embed.add_field(name="DM",          value=dm_status,                inline=False)
        original_embed.add_field(name="Reviewed By", value=interaction.user.mention, inline=False)

        await interaction.message.edit(embed=original_embed, view=self)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"[Apply] ReviewView error: {error}")
        try:
            await interaction.followup.send(
                "❌ Something went wrong processing this application. Please try again.",
                ephemeral=True
            )
        except Exception:
            pass


@bot.tree.command(name="apply", description="Apply for a staff position at Winstree Academy")
@cooldown()
async def apply(interaction: discord.Interaction):
    discord_id = str(interaction.user.id)
    can_apply, block_reason = await check_application_eligibility(discord_id)

    if not can_apply:
        print(f"[Apply] Blocked {interaction.user} (ID: {discord_id}) at /apply stage — reason: {block_reason}")

        await _send_blocked_notification(
            client=interaction.client,
            user=interaction.user,
            discord_id=discord_id,
            block_reason=block_reason,
            stage="/apply command",
        )

        return await interaction.response.send_message(
            "Oops! You can't apply for that position right now. Try again later!",
            ephemeral=True,
        )

    await interaction.response.send_message(
        "## 📋 Winstree Academy — Staff Application\n"
        "Please select your age range below to begin your application.\n\n"
        "_ _",
        view=AgeRangeView(),
        ephemeral=True,
    )


# -------------------------------------------------
#  TEXT COMMAND FALLBACK
# -------------------------------------------------
@bot.command(name='readsheet')
async def text_readsheet(ctx, sheet_name: str = "Sheet1"):
    try:
        all_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(sheet_name).get_all_values()
        )
        if not all_data:
            await ctx.send("The sheet is empty!")
            return
        response = f"**Data from '{sheet_name}':**\n```\n"
        for row in all_data[:10]:
            response += " | ".join(str(cell) for cell in row) + "\n"
        response += "```"
        if len(all_data) > 10:
            response += f"\n*Showing first 10 of {len(all_data)} rows*"
        await ctx.send(response)
    except Exception as e:
        await ctx.send(f"Error: {e}")


# -------------------------------------------------
#  RUN — with login retry back-off
# -------------------------------------------------
if __name__ == '__main__':
    keep_alive()

    async def main():
        async with bot:
            await bot.start(DISCORD_TOKEN)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot shutting down…")
