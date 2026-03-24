import discord
from discord import app_commands
from discord.ext import commands, tasks
import gspread
from google.oauth2.service_account import Credentials
import aiohttp
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
    "pong":                  3,
    "setstatus":            10,
    "diagnoseroblox":       30,
    "openroles": 10,
}

def cooldown(command_name: str | None = None):
    """Decorator — blocks reuse until cooldown expires. Place directly below @bot.tree.command."""
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

# FIX #3: shared aiohttp session created once at startup instead of
# opening a new ClientSession on every single HTTP call.
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

SELF_PING_INTERVAL = 300
SELF_PING_CHANNEL_ID = os.getenv('SELF_PING_CHANNEL_ID')

ACADEMY_TIMETABLE_CHANNEL_ID      = 0
SIXTH_FORM_TIMETABLE_CHANNEL_ID   = 0
ACADEMY_STAFF_ROLE_ID             = 1438203308178145310
SIXTH_FORM_STAFF_ROLE_ID          = 1459540584414052373
TIMETABLE_POST_TIME               = "21:15"
YEAR_LEADER_ROLE_ID               = 0
TIMETABLE_ADMIN_ROLE_ID           = 1438202314476228678

ROBLOX_COOKIE   = os.getenv('ROBLOX_AUTH_TOKEN')
ROBLOX_GROUP_ID = "32528351"

ROLE_NAME_MAP = {
    "School Staff":                               "Teaching Staff",
    "Deputy Head of Year 7":                       "Deputy Head of Year",
    "Deputy Head of Year 8":                       "Deputy Head of Year",
    "Deputy Head of Year 9":                       "Deputy Head of Year",
    "Deputy Head of Year 10":                      "Deputy Head of Year",
    "Deputy Head of Year 11":                      "Deputy Head of Year",
    "Deputy Head of Sixth Form":                   "Deputy Head of Year",
    "Head of Year 7":                              "Head of Year",
    "Head of Year 8":                              "Head of Year",
    "Head of Year 9":                              "Head of Year",
    "Head of Year 10":                             "Head of Year",
    "Head of Year 11":                             "Head of Year",
    "Head of Sixth Form":                          "Head of Year",
    "Deputy Head of Lower Level":                  "Deputy Head of Level",
    "Deputy Head of Middle Level":                  "Deputy Head of Level",
    "Deputy Head of Upper Level":                  "Deputy Head of Level",
    "Head of Lower Level":                         "Head of Level",
    "Head of Middle Level":                        "Head of Level",
    "Head of Upper Level":                         "Head of Level",
    "Site Staff":                                  "Site Staff",
    "Site Executive":                              "Site Operations Executive",
    "Assistant Headteacher":                       "Assistant Headteacher",
    "Deputy Headteacher":                          "Deputy Headteacher",
    "Headteacher":                                 "Headteacher",
    "Executive Headteacher":                       "Executive Headteacher",
    "Chief Education Officer":                     "Chief Education Officer",
}

# Roles with unlimited openings — everything else has exactly 1 spot
INFINITE_ROLES = {
    "School Staff"
}

ALL_STAFF_SHEET     = "All Staff"
CURRENT_STAFF_SHEET = "Current Staff"
ATTENDANCE_SHEET    = "Attendance Register"
ROLES_LIST_SHEET    = "Roles List"
EDIT_STAFF_SHEET    = "Edit Staff"

ALL_STAFF_DATA_START = 5
ALL_STAFF_NAME_COL   = 3

CURRENT_STAFF_DATA_START     = 5
CURRENT_STAFF_NAME_COL       = 3
CURRENT_STAFF_ATTENDANCE_COL = 9

ATTEND_DATA_START  = 5
ATTEND_NAME_COL    = 1
ATTEND_CHECK_START = 2
ATTEND_CHECK_END   = 9

FIELD_MAP = {
    "role":            ("Role",             "C6"),
    "roblox_username": ("Roblox Username",  "D6"),
    "teaching_name":   ("Teaching Name",    "E6"),
    "area":            ("Area",             "F6"),
    "discord_id":      ("Discord User ID",  "I6"),
}

print("Starting bot...", flush=True)
print(f"Token present: {bool(DISCORD_TOKEN)}", flush=True)
print(f"Google credentials present: {bool(os.getenv('GOOGLE_CREDENTIALS'))}", flush=True)
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
            # Used on hosted platforms (Render, Railway, etc.)
            creds_dict = json.loads(credentials_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        else:
            # Used locally — load from credentials.json file
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
    now = datetime.now()
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

    # FIX #2: run the initial cache refresh through the executor so it
    # doesn't block the event loop, and guard it so it only fires once
    # rather than on every reconnect (which hammers the Sheets API).
    if not hasattr(bot, '_cache_loaded'):
        bot._cache_loaded = True
        asyncio.get_event_loop().run_in_executor(None, refresh_staff_names_cache)

    last_ping_time    = datetime.now()
    last_ping_latency = round(bot.latency * 1000)

    await bot.change_presence(activity=discord.CustomActivity(name="Winstree Academy's Assistant. Run /hello to try me out!"))

    # FIX #1: only sync commands once per process lifetime, not on every
    # reconnect. Discord heavily rate-limits tree.sync() calls.
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
        worksheet = spreadsheet.worksheet(ALL_STAFF_SHEET)
        all_data  = worksheet.get_all_values()
        data_rows = all_data[ALL_STAFF_DATA_START - 1:]
        staff_names_cache = [
            row[ALL_STAFF_NAME_COL].strip()
            for row in data_rows
            if len(row) > ALL_STAFF_NAME_COL and row[ALL_STAFF_NAME_COL].strip()
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
        choices = ["Academy", "Sixth Form"]
        filtered = [c for c in choices if current.lower() in c.lower()]
        return [app_commands.Choice(name=c, value=c) for c in filtered]
    return []


# -------------------------------------------------
#  ROBLOX HELPERS
# -------------------------------------------------
async def get_roblox_user_id(username: str) -> int | None:
    try:
        session = await get_http_session()
        async with session.post(
            "https://users.roblox.com/v1/usernames/users",
            json={"usernames": [username], "excludeBannedUsers": False}
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("data"):
                    return data["data"][0]["id"]
    except Exception as e:
        print(f"[Roblox] User ID lookup error: {e}")
    return None


async def get_group_role_id(role_name: str) -> int | None:
    roblox_role_name = ROLE_NAME_MAP.get(role_name)
    if not roblox_role_name:
        return None
    try:
        session = await get_http_session()
        async with session.get(f"https://groups.roblox.com/v1/groups/{ROBLOX_GROUP_ID}/roles") as resp:
            if resp.status == 200:
                data = await resp.json()
                for role in data.get("roles", []):
                    if role["name"].lower() == roblox_role_name.lower():
                        return role["id"]
    except Exception as e:
        print(f"[Roblox] Role ID lookup error: {e}")
    return None


async def get_rank_1_role_id() -> int | None:
    try:
        session = await get_http_session()
        async with session.get(f"https://groups.roblox.com/v1/groups/{ROBLOX_GROUP_ID}/roles") as resp:
            if resp.status == 200:
                data = await resp.json()
                roles = data.get("roles", [])
                rank_1 = next((r for r in roles if r["rank"] == 1), None)
                if rank_1:
                    return rank_1["id"]
                if roles:
                    return sorted(roles, key=lambda r: r["rank"])[0]["id"]
    except Exception as e:
        print(f"[Roblox] Rank 1 role ID error: {e}")
    return None


async def set_user_group_role(roblox_user_id: int, role_id: int) -> tuple[bool, str]:
    if not ROBLOX_COOKIE or not ROBLOX_GROUP_ID:
        return False, "ROBLOX_COOKIE or ROBLOX_GROUP_ID not configured"
    try:
        cookie = ROBLOX_COOKIE.strip()
        cookie_header = {"Cookie": f".ROBLOSECURITY={cookie}"}
        session = await get_http_session()
        async with session.patch(
            f"https://groups.roblox.com/v1/groups/{ROBLOX_GROUP_ID}/users/{roblox_user_id}",
            json={"roleId": role_id},
            headers={**cookie_header, "Content-Type": "application/json"}
        ) as first_resp:
            csrf = first_resp.headers.get("x-csrf-token", "").strip()
            if first_resp.status == 200:
                return True, ""
        if not csrf:
            return False, "Roblox did not return a CSRF token"
        async with session.patch(
            f"https://groups.roblox.com/v1/groups/{ROBLOX_GROUP_ID}/users/{roblox_user_id}",
            json={"roleId": role_id},
            headers={**cookie_header, "Content-Type": "application/json", "X-CSRF-TOKEN": csrf}
        ) as resp:
            if resp.status == 200:
                return True, ""
            text = await resp.text()
            return False, f"HTTP {resp.status}: {text[:200]}"
    except Exception as e:
        return False, str(e)


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


# -------------------------------------------------
#  TIMETABLE COMMANDS
# -------------------------------------------------
@bot.tree.command(name="bookacademy", description="Book a slot on the Academy timetable")
@app_commands.describe(year="Select the year group", period="Select the period to book", subject="Enter the subject name", room="Enter the room number/name")
@app_commands.choices(year=[
    app_commands.Choice(name="Year 7", value="Year 7"),
    app_commands.Choice(name="Year 8", value="Year 8"),
    app_commands.Choice(name="Year 9", value="Year 9"),
    app_commands.Choice(name="Year 10", value="Year 10"),
    app_commands.Choice(name="Year 11", value="Year 11"),
    app_commands.Choice(name="Additional Needs Unit", value="Additional Needs Unit"),
    app_commands.Choice(name="Isolation", value="Isolation"),
    app_commands.Choice(name="Detention", value="Detention"),
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
    embed.add_field(name="Year Group", value=year, inline=True)
    embed.add_field(name="Period", value=period, inline=True)
    embed.add_field(name="Subject", value=subject, inline=True)
    embed.add_field(name="Room", value=room, inline=True)
    embed.add_field(name="Booked By", value=teaching_name, inline=True)
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
    app_commands.Choice(name="Year 12", value="Year 12"),
    app_commands.Choice(name="Year 13", value="Year 13"),
    app_commands.Choice(name="Additional Needs Unit", value="Additional Needs Unit"),
    app_commands.Choice(name="Isolation", value="Isolation"),
    app_commands.Choice(name="Detention", value="Detention"),
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
    embed.add_field(name="Year Group", value=year, inline=True)
    embed.add_field(name="Period", value=period, inline=True)
    embed.add_field(name="Subject", value=subject, inline=True)
    embed.add_field(name="Room", value=room, inline=True)
    embed.add_field(name="Booked By", value=teaching_name, inline=True)
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
    app_commands.Choice(name="Year 7", value="Year 7"),
    app_commands.Choice(name="Year 8", value="Year 8"),
    app_commands.Choice(name="Year 9", value="Year 9"),
    app_commands.Choice(name="Year 10", value="Year 10"),
    app_commands.Choice(name="Year 11", value="Year 11"),
    app_commands.Choice(name="Additional Needs Unit", value="Additional Needs Unit"),
    app_commands.Choice(name="Isolation", value="Isolation"),
    app_commands.Choice(name="Detention", value="Detention"),
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
    is_admin = has_timetable_admin_role(interaction)

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
    embed.add_field(name="Year Group", value=year, inline=True)
    embed.add_field(name="Period", value=period, inline=True)
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
    app_commands.Choice(name="Year 12", value="Year 12"),
    app_commands.Choice(name="Year 13", value="Year 13"),
    app_commands.Choice(name="Additional Needs Unit", value="Additional Needs Unit"),
    app_commands.Choice(name="Isolation", value="Isolation"),
    app_commands.Choice(name="Detention", value="Detention"),
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
    is_admin = has_timetable_admin_role(interaction)

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
    embed.add_field(name="Year Group", value=year, inline=True)
    embed.add_field(name="Period", value=period, inline=True)
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
        # Fetch every role from the Roles List sheet
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

        # Fetch current staff to count how many people hold each role
        current_data = await safe_sheets_call(
            lambda: spreadsheet.worksheet(CURRENT_STAFF_SHEET).get_all_values()
        )

        role_counts: dict[str, int] = {}
        for row in current_data[CURRENT_STAFF_DATA_START - 1:]:
            if len(row) > 1:
                role = row[1].strip()   # Column B = Role
                if role:
                    role_counts[role] = role_counts.get(role, 0) + 1

        # Split roles into three buckets
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

        # ---- Build embed ----
        embed = discord.Embed(
            title="📋 Staff Role Openings",
            color=discord.Color.blue(),
            timestamp=datetime.now(),
        )

        if singleton_open:
            # Chunk into ≤1024-char fields if there are many roles
            chunk, chunks = "", []
            for role in singleton_open:
                line = f"• {role}\n"
                if len(chunk) + len(line) > 1000:
                    chunks.append(chunk.strip())
                    chunk = ""
                chunk += line
            if chunk:
                chunks.append(chunk.strip())

            for i, c in enumerate(chunks):
                label = f"✅ Open Positions ({len(singleton_open)})" if i == 0 else "✅ Open Positions (cont.)"
                embed.add_field(name=label, value=c, inline=False)
        else:
            embed.add_field(name="✅ Open Positions", value="*All positions are currently filled.*", inline=False)

        if infinite_roles:
            embed.add_field(
                name="♾️ Always Hiring (Unlimited Spots)",
                value="\n".join(f"• {r}" for r in infinite_roles),
                inline=False,
            )

        if singleton_filled:
            chunk, chunks = "", []
            for role in singleton_filled:
                line = f"• {role}\n"
                if len(chunk) + len(line) > 1000:
                    chunks.append(chunk.strip())
                    chunk = ""
                chunk += line
            if chunk:
                chunks.append(chunk.strip())

            for i, c in enumerate(chunks):
                label = f"❌ Filled Positions ({len(singleton_filled)})" if i == 0 else "❌ Filled Positions (cont.)"
                embed.add_field(name=label, value=c, inline=False)

        embed.set_footer(
            text=f"{len(singleton_open)} open · {len(singleton_filled)} filled · "
                 f"{len(infinite_roles)} unlimited"
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
@bot.tree.command(name="edit", description="Edit a staff member's details in the Edit Staff sheet")
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
            embed.set_footer(text="Edit Staff sheet has been updated")

            if field == "role":
                roblox_username = await get_roblox_username_for_staff(staff_name)
                if roblox_username:
                    roblox_user_id = await get_roblox_user_id(roblox_username)
                    role_id        = await get_group_role_id(value)
                    if roblox_user_id and role_id:
                        success, err = await set_user_group_role(roblox_user_id, role_id)
                        embed.add_field(name="Roblox Group", value=f"✅ Rank updated to **{value}**" if success else f"⚠️ Could not update rank: {err}", inline=False)
                    elif not roblox_user_id:
                        embed.add_field(name="Roblox Group", value=f"⚠️ Roblox user **{roblox_username}** not found", inline=False)
                    else:
                        embed.add_field(name="Roblox Group", value=f"⚠️ Role **{ROLE_NAME_MAP.get(value, value)}** not found in group", inline=False)
                else:
                    embed.add_field(name="Roblox Group", value="⚠️ No Roblox username on file", inline=False)

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
            embed.add_field(name="Staff Member",      value=staff_name,    inline=True)
            embed.add_field(name="Remaining Strikes", value=new_strikes,   inline=True)
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
    params = {"action": "loa", "staffName": staff_name, "loaValue": loa_value, "loaReason": reason.strip()}

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
            lambda: spreadsheet.worksheet(ALL_STAFF_SHEET).get_all_values()
        )

        current_level = None
        for row in all_data[ALL_STAFF_DATA_START - 1:]:
            if len(row) > ALL_STAFF_NAME_COL and row[ALL_STAFF_NAME_COL].strip().lower() == staff_name.lower():
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
        await interaction.followup.send(f"❌ Sheet '{ALL_STAFF_SHEET}' not found!")
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
async def hire(interaction: discord.Interaction, teaching_name: str, roblox_username: str, discord_account: discord.Member, area: str, role: str):
    await interaction.response.defer()
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
            embed = discord.Embed(title="New Staff Member Hired!", color=discord.Color.green())
            embed.add_field(name="Teaching Name",   value=teaching_name,           inline=True)
            embed.add_field(name="Roblox Username", value=roblox_username,         inline=True)
            embed.add_field(name="Discord Account", value=discord_account.mention, inline=True)
            embed.add_field(name="Discord ID",      value=discord_user_id,         inline=True)
            embed.add_field(name="Area",            value=area,                    inline=True)
            embed.add_field(name="Role",            value=role,                    inline=True)
            embed.set_footer(text="Edit Staff sheet has been updated")

            roblox_user_id = await get_roblox_user_id(roblox_username)
            role_id        = await get_group_role_id(role)
            if roblox_user_id and role_id:
                success, err = await set_user_group_role(roblox_user_id, role_id)
                embed.add_field(name="Roblox Group", value=f"✅ Rank set to **{role}**" if success else f"⚠️ Could not set rank: {err}", inline=False)
            elif not roblox_user_id:
                embed.add_field(name="Roblox Group", value=f"⚠️ Roblox user **{roblox_username}** not found", inline=False)
            else:
                embed.add_field(name="Roblox Group", value=f"⚠️ Role **{ROLE_NAME_MAP.get(role, role)}** not found in group", inline=False)

            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"Apps Script error (HTTP {status}):\n```{response_text[:500]}```")
    except aiohttp.ClientError as e:
        await interaction.followup.send(f"Could not reach Apps Script: {e}")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")


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
            embed.set_footer(text="Edit Staff sheet has been updated")

            roblox_username = await get_roblox_username_for_staff(staff_name)
            if roblox_username:
                roblox_user_id = await get_roblox_user_id(roblox_username)
                rank_1_id      = await get_rank_1_role_id()
                if roblox_user_id and rank_1_id:
                    success, err = await set_user_group_role(roblox_user_id, rank_1_id)
                    embed.add_field(name="Roblox Group", value="✅ Demoted to rank 1" if success else f"⚠️ Could not demote: {err}", inline=False)
                elif not roblox_user_id:
                    embed.add_field(name="Roblox Group", value=f"⚠️ Roblox user **{roblox_username}** not found", inline=False)
                else:
                    embed.add_field(name="Roblox Group", value="⚠️ Could not find rank 1 role", inline=False)
            else:
                embed.add_field(name="Roblox Group", value="⚠️ No Roblox username on file", inline=False)

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

        teaching_name = safe_get(staff_row_data, CURRENT_STAFF_NAME_COL)
        strikes_value = safe_get(staff_row_data, 8, "0")

        embed = discord.Embed(title="Your Staff Profile", description=f"**{teaching_name}**", color=discord.Color.blue())
        embed.add_field(name="Role",            value=safe_get(staff_row_data, 1),      inline=True)
        embed.add_field(name="Roblox Username", value=safe_get(staff_row_data, 2),      inline=True)
        embed.add_field(name="Area",            value=safe_get(staff_row_data, 4),      inline=True)
        embed.add_field(name="Training Level",  value=safe_get(staff_row_data, 5, "0"), inline=True)
        loa_value = safe_get(staff_row_data, 6)
        embed.add_field(name="LOA",             value=loa_value,                        inline=True)
        embed.add_field(name="Strikes",         value=strikes_value,                    inline=True)

        try:
            all_staff_data = await safe_sheets_call(
                lambda: spreadsheet.worksheet(ALL_STAFF_SHEET).get_all_values()
            )
            ws = spreadsheet.worksheet(ALL_STAFF_SHEET)
            for i, row in enumerate(all_staff_data[ALL_STAFF_DATA_START - 1:], start=ALL_STAFF_DATA_START):
                if len(row) > ALL_STAFF_NAME_COL and row[ALL_STAFF_NAME_COL].strip().lower() == teaching_name.lower():
                    strike_cell = await safe_sheets_call(lambda: ws.cell(i, 9))
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
                        loa_cell = await safe_sheets_call(lambda: ws.cell(i, 7))
                        if loa_cell.note:
                            embed.add_field(name="LOA Reason", value=loa_cell.note, inline=False)
                    break
        except Exception as e:
            print(f"Error fetching reasons: {e}")

        embed.add_field(name="Attendance", value=f"{safe_get(staff_row_data, CURRENT_STAFF_ATTENDANCE_COL, '0')} sessions", inline=False)
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
            embed.set_footer(text="Edit Staff sheet has been updated")
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
        embed.add_field(name="Attendance",          value=f"{safe_get(staff_row_data, CURRENT_STAFF_ATTENDANCE_COL, '0')} sessions", inline=True)

        try:
            all_staff_data = await safe_sheets_call(
                lambda: spreadsheet.worksheet(ALL_STAFF_SHEET).get_all_values()
            )
            ws = spreadsheet.worksheet(ALL_STAFF_SHEET)
            for i, row in enumerate(all_staff_data[ALL_STAFF_DATA_START - 1:], start=ALL_STAFF_DATA_START):
                if len(row) > ALL_STAFF_NAME_COL and row[ALL_STAFF_NAME_COL].strip().lower() == staff_name.lower():
                    strike_cell = await safe_sheets_call(lambda: ws.cell(i, 9))
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
                        loa_cell = await safe_sheets_call(lambda: ws.cell(i, 7))
                        if loa_cell.note:
                            embed.add_field(name="LOA Reason", value=loa_cell.note, inline=False)
                    break
        except Exception as e:
            print(f"Error fetching reasons: {e}")

        if loa_value != "N/A":
            embed.add_field(name="Leave Date",          value=safe_get(staff_row_data, 10), inline=True)
            embed.add_field(name="Reason of Departure", value=safe_get(staff_row_data, 11), inline=True)

        embed.set_footer(text=f"Row {staff_row_idx} | {CURRENT_STAFF_SHEET}")
        await interaction.followup.send(embed=embed)

    except gspread.WorksheetNotFound:
        await interaction.followup.send(f"Sheet '{CURRENT_STAFF_SHEET}' not found!")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")


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
#  /resetattendance
# -------------------------------------------------
@bot.tree.command(name="resetattendance", description="Reset all attendance records to 0 on All Staff sheet")
@cooldown()
async def reset_attendance(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        def _reset():
            worksheet    = spreadsheet.worksheet(ALL_STAFF_SHEET)
            last_row     = worksheet.row_count
            cell_range   = worksheet.range(f"J5:J{last_row}")
            for cell in cell_range:
                cell.value = 0
            worksheet.update_cells(cell_range)
            return len(cell_range)

        rows_reset = await safe_sheets_call(_reset)

        embed = discord.Embed(title="Attendance Reset Complete!", color=discord.Color.green())
        embed.add_field(name="Sheet",      value=ALL_STAFF_SHEET,               inline=True)
        embed.add_field(name="Column",     value="J (Attendance)",              inline=True)
        embed.add_field(name="Rows Reset", value=f"{rows_reset} rows set to 0", inline=True)
        embed.set_footer(text=f"All attendance in {ALL_STAFF_SHEET} has been reset to 0")
        await interaction.followup.send(embed=embed)

    except gspread.WorksheetNotFound:
        await interaction.followup.send(f"Sheet '{ALL_STAFF_SHEET}' not found!")
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
#  /ping  /pong
# -------------------------------------------------
@bot.tree.command(name="ping", description="Check latency")
@cooldown()
async def slash_ping(interaction: discord.Interaction):
    await interaction.response.send_message(f'Pong! Latency: {round(bot.latency * 1000)}ms')

@bot.tree.command(name="hello", description="Responds with Ping")
@cooldown()
async def slash_pong(interaction: discord.Interaction):
    await interaction.response.send_message('Goodbye')


# -------------------------------------------------
#  /diagnoseroblox
# -------------------------------------------------
@bot.tree.command(name="diagnoseroblox", description="[ADMIN] Run Roblox authentication diagnostics")
@cooldown()
async def diagnose_roblox(interaction: discord.Interaction):
    await interaction.response.send_message("🔍 Checking environment...", ephemeral=True)

    output = ["=" * 50, "ENVIRONMENT DEBUG", "=" * 50, "\n[1] Checking environment variables..."]

    cookie_from_env      = os.getenv('ROBLOX_COOKIE')
    roblosecurity_from_env = os.getenv('.ROBLOSECURITY')
    roblosecurity_alt    = os.getenv('ROBLOSECURITY')

    output.append(f"ROBLOX_COOKIE exists: {cookie_from_env is not None}")
    if cookie_from_env:
        output.append(f"  Length: {len(cookie_from_env)}")
        output.append(f"  First 80: {cookie_from_env[:80]}")

    output.append(f"\n.ROBLOSECURITY exists: {roblosecurity_from_env is not None}")
    if roblosecurity_from_env:
        output.append(f"  Length: {len(roblosecurity_from_env)}")

    output.append(f"\nROBLOSECURITY exists: {roblosecurity_alt is not None}")
    if roblosecurity_alt:
        output.append(f"  Length: {len(roblosecurity_alt)}")

    output.append(f"\n[2] What the bot code sees...")
    output.append(f"ROBLOX_COOKIE variable: {ROBLOX_COOKIE is not None}")
    if ROBLOX_COOKIE:
        output.append(f"  Length: {len(ROBLOX_COOKIE)}")
        output.append(f"  First 80: {ROBLOX_COOKIE[:80]}")

    output.append(f"\n[3] Checking for .env file...")
    env_exists = os.path.exists('.env')
    output.append(f".env file exists: {env_exists}")
    if env_exists:
        try:
            with open('.env', 'r') as f:
                for line in f.readlines()[:10]:
                    if 'ROBLOX' in line or 'SECURITY' in line:
                        parts = line.split('=', 1)
                        if len(parts) == 2:
                            output.append(f"  {parts[0]}=[{len(parts[1])} chars]")
        except Exception:
            output.append("  Could not read .env file")

    output.append("\n" + "=" * 50)
    full_output = "\n".join(output)

    chunks = [full_output[i:i+1900] for i in range(0, len(full_output), 1900)]
    await interaction.edit_original_response(content=f"```\n{chunks[0]}\n```")
    for chunk in chunks[1:]:
        await interaction.followup.send(f"```\n{chunk}\n```", ephemeral=True)


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
async def run_bot():
    delay = 5
    max_delay = 300
    attempt = 0

    while True:
        attempt += 1
        try:
            print(f"[Startup] Login attempt {attempt}…", flush=True)
            async with bot:
                await bot.start(DISCORD_TOKEN)

        except discord.errors.HTTPException as e:
            if e.status == 429:
                retry_after = float(getattr(e.response, 'headers', {}).get('Retry-After', delay))
                wait = max(retry_after, delay)
                print(f"[Startup] 429 rate-limited on login. Waiting {wait:.0f}s before retry…", flush=True)
                await asyncio.sleep(wait)
                delay = min(delay * 2, max_delay)
            else:
                print(f"[Startup] HTTP error during login: {e}", flush=True)
                raise

        except discord.errors.LoginFailure:
            print("[Startup] Invalid Discord token — check your DISCORD_TOKEN env var.", flush=True)
            raise

        except Exception as e:
            print(f"[Startup] Unexpected error: {e}. Retrying in {delay}s…", flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)


if __name__ == '__main__':
    keep_alive()

    async def main():
        async with bot:
            await bot.start(DISCORD_TOKEN)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot shutting down…")
