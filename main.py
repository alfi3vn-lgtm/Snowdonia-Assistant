import discord
from discord import app_commands
from discord.ext import commands
import gspread
from google.oauth2.service_account import Credentials
import aiohttp
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
SPREADSHEET_ID   = '1fUkh8LhRhRqQq9MjlgzM4bI2sIhbkvmTrMYFehqNJMs'
DISCORD_TOKEN    = os.getenv('DISCORD_TOKEN')  # ← Load from .env
CREDENTIALS_FILE = 'credentials.json'

# ── Apps Script Web App URL (handles writing to the Edit Staff sheet) ──
APPS_SCRIPT_URL = 'https://script.google.com/macros/s/AKfycbwiKn7Xo_nGyfRvtH3z8LEPYPbxXOKjvM8DRCfbg2gbYO5jSfUEv9ZT5unVUaJoVIk/exec'

# ── Sheet / column settings ──
ALL_STAFF_SHEET   = "All Staff"
ATTENDANCE_SHEET  = "Attendance Register"
ROLES_LIST_SHEET  = "Roles List"
EDIT_STAFF_SHEET  = "Edit Staff"

ALL_STAFF_DATA_START = 5
ALL_STAFF_NAME_COL   = 3   # Column D → 0-based index 3

ATTEND_DATA_START    = 5
ATTEND_NAME_COL      = 1   # Column B → 0-based index 1
ATTEND_CHECK_START   = 2   # Column C → index 2
ATTEND_CHECK_END     = 9   # One past column I

# ── Field → (Display Label, Target Cell) mapping ──
FIELD_MAP = {
    "position":       ("Position",         "C6"),
    "staff_username": ("Staff Username",   "D6"),
    "teaching_name":  ("Teaching Name",    "E6"),
    "area":           ("Area",             "F6"),
    "training_level": ("Training Level",   "G6"),
    "loa":            ("Leave of Absence", "H6"),
    "notes":          ("Notes",            "I6"),
    "strikes":        ("Strikes",          "J6"),
}

# ─────────────────────────────────────────────
#  BOT SETUP
# ─────────────────────────────────────────────
SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

sheets_client = None
spreadsheet   = None


def setup_google_sheets():
    global sheets_client, spreadsheet
    try:
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
        sheets_client = gspread.authorize(creds)
        spreadsheet   = sheets_client.open_by_key(SPREADSHEET_ID)
        print("✅ Connected to Google Sheets!")
        return True
    except Exception as e:
        print(f"❌ Google Sheets connection failed: {e}")
        return False


@bot.event
async def on_ready():
    print(f'{bot.user} connected to Discord!')
    print(f'In {len(bot.guilds)} server(s)')
    setup_google_sheets()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def is_checked(cell_value: str) -> bool:
    """Google Sheets checkboxes return the string TRUE or FALSE."""
    return str(cell_value).strip().upper() in {'TRUE', 'YES', 'Y', '1', 'X', '✓', '✔'}


def safe_get(row: list, index: int, default: str = "N/A") -> str:
    if index < len(row) and str(row[index]).strip():
        return str(row[index]).strip()
    return default


# ─────────────────────────────────────────────
#  AUTOCOMPLETE – Staff names (All Staff sheet)
# ─────────────────────────────────────────────
async def staff_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    try:
        worksheet = spreadsheet.worksheet(ALL_STAFF_SHEET)
        all_data  = worksheet.get_all_values()
        data_rows = all_data[ALL_STAFF_DATA_START - 1:]

        staff_names = [
            row[ALL_STAFF_NAME_COL].strip()
            for row in data_rows
            if len(row) > ALL_STAFF_NAME_COL and row[ALL_STAFF_NAME_COL].strip()
        ]

        filtered = [n for n in staff_names if current.lower() in n.lower()]
        return [app_commands.Choice(name=n, value=n) for n in filtered[:25]]
    except Exception:
        return []


# ─────────────────────────────────────────────
#  AUTOCOMPLETE – Staff names from B5 dropdown
#  (Edit Staff sheet validation list)
# ─────────────────────────────────────────────
async def edit_staff_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """
    Pulls the dropdown options for cell B5 from the Edit Staff sheet.
    Falls back to the All Staff sheet if the validation list can't be read.
    """
    try:
        # The B5 dropdown is typically sourced from the All Staff sheet names,
        # so we reuse the same source list for maximum accuracy.
        worksheet = spreadsheet.worksheet(ALL_STAFF_SHEET)
        all_data  = worksheet.get_all_values()
        data_rows = all_data[ALL_STAFF_DATA_START - 1:]

        staff_names = [
            row[ALL_STAFF_NAME_COL].strip()
            for row in data_rows
            if len(row) > ALL_STAFF_NAME_COL and row[ALL_STAFF_NAME_COL].strip()
        ]

        filtered = [n for n in staff_names if current.lower() in n.lower()]
        return [app_commands.Choice(name=n, value=n) for n in filtered[:25]]
    except Exception:
        return []


# ─────────────────────────────────────────────
#  AUTOCOMPLETE – Positions (from Roles List sheet)
# ─────────────────────────────────────────────
async def position_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    try:
        worksheet = spreadsheet.worksheet(ROLES_LIST_SHEET)
        all_data  = worksheet.get_all_values()

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


# ─────────────────────────────────────────────
#  AUTOCOMPLETE – Edit value (context-sensitive)
#  Reads the 'field' parameter already entered and
#  returns appropriate choices for that field type.
# ─────────────────────────────────────────────
async def edit_value_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    # Detect which field is currently selected in the same command invocation
    field = None
    try:
        for opt in interaction.data.get('options', []):
            if opt.get('name') == 'field':
                field = opt.get('value')
                break
    except Exception:
        pass

    if field == 'position':
        # Delegate to the Roles List autocomplete
        return await position_autocomplete(interaction, current)

    elif field == 'area':
        choices = ["Academy", "Sixth Form"]
        filtered = [c for c in choices if current.lower() in c.lower()]
        return [app_commands.Choice(name=c, value=c) for c in filtered]

    elif field == 'training_level':
        choices = ["Level 1", "Level 2", "Level 3"]
        filtered = [c for c in choices if current.lower() in c.lower()]
        return [app_commands.Choice(name=c, value=c) for c in filtered]

    # For strikes: no value box needed (auto-increments)
    # For staff_username / teaching_name / loa / notes: free-text, no suggestions
    return []


# ─────────────────────────────────────────────
#  /edit – Edit a staff member's details
# ─────────────────────────────────────────────
@bot.tree.command(
    name="edit",
    description="Edit a staff member's details in the Edit Staff sheet"
)
@app_commands.describe(
    staff_name = "Select the staff member to edit  (matches B5 dropdown)",
    field      = "Which field to update",
    value      = "New value — leave empty for Strikes (auto-increments by 1)",
)
@app_commands.choices(field=[
    app_commands.Choice(name="Position",         value="position"),
    app_commands.Choice(name="Staff Username",   value="staff_username"),
    app_commands.Choice(name="Teaching Name",    value="teaching_name"),
    app_commands.Choice(name="Area",             value="area"),
    app_commands.Choice(name="Training Level",   value="training_level"),
    app_commands.Choice(name="Leave of Absence", value="loa"),
    app_commands.Choice(name="Notes",            value="notes"),
    app_commands.Choice(name="Strikes",          value="strikes"),
])
@app_commands.autocomplete(staff_name=edit_staff_autocomplete, value=edit_value_autocomplete)
async def edit_staff(
    interaction: discord.Interaction,
    staff_name: str,
    field:      str,
    value:      str = None,
):
    await interaction.response.defer()

    field_label, target_cell = FIELD_MAP.get(field, (field, "?"))

    # ── Strikes: read current value then auto-increment ──────────────────
    if field == "strikes":
        try:
            worksheet = spreadsheet.worksheet(ALL_STAFF_SHEET)
            all_data  = worksheet.get_all_values()
            new_strikes = 1  # default if not found or blank

            for row in all_data[ALL_STAFF_DATA_START - 1:]:
                if (
                    len(row) > ALL_STAFF_NAME_COL
                    and row[ALL_STAFF_NAME_COL].strip().lower() == staff_name.lower()
                ):
                    raw = safe_get(row, 8, "0")   # index 8 = Strikes column
                    try:
                        new_strikes = int(raw) + 1
                    except ValueError:
                        new_strikes = 1
                    break

            value = str(new_strikes)

        except Exception as e:
            await interaction.followup.send(
                f"❌ Could not read current strikes for **{staff_name}**: {e}"
            )
            return

    # ── All other fields: value is required ──────────────────────────────
    if not value:
        await interaction.followup.send(
            f"❌ A value is required when editing **{field_label}**. "
            f"Please re-run `/edit` and fill in the **value** box."
        )
        return

    # ── Call the Apps Script web app ─────────────────────────────────────
    #
    # The Apps Script (doGet) should handle action="edit" by:
    #   1. Setting B5  → staffName    (selects the staff member in the sheet)
    #   2. Setting the target cell    → value
    #   3. Triggering the save button
    #
    params = {
        "action":    "edit",
        "staffName": staff_name,
        "field":     field,
        "cell":      target_cell,
        "value":     value,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                APPS_SCRIPT_URL, params=params, allow_redirects=True
            ) as resp:
                response_text = await resp.text()
                status        = resp.status

        if status == 200 and "error" not in response_text.lower():
            embed = discord.Embed(
                title="✅ Staff Record Updated!",
                color=discord.Color.green()
            )
            embed.add_field(name="Staff Member",  value=staff_name,                    inline=True)
            embed.add_field(name="Field Updated", value=f"{field_label} ({target_cell})", inline=True)
            embed.add_field(name="New Value",     value=value,                         inline=True)
            embed.set_footer(text="Edit Staff sheet has been updated ✔")
            await interaction.followup.send(embed=embed)

        else:
            await interaction.followup.send(
                f"⚠️ Apps Script returned an unexpected response (HTTP {status}):\n"
                f"```{response_text[:500]}```\n"
                f"Check your Apps Script logs for details."
            )

    except aiohttp.ClientError as e:
        await interaction.followup.send(
            f"❌ Could not reach the Apps Script web app: {e}"
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Unexpected error during edit: {e}")


# ─────────────────────────────────────────────
#  /hire – Hire a new staff member
# ─────────────────────────────────────────────
@bot.tree.command(name="hire", description="Hire a new staff member and add them to the Edit Staff sheet")
@app_commands.describe(
    teaching_name  = "The staff member's teaching name  →  goes into E6",
    staff_username = "The staff member's username        →  goes into D6",
    area           = "Academy or Sixth Form              →  selected on F6 dropdown",
    position       = "Their role from the Roles List     →  selected on C6 dropdown",
)
@app_commands.choices(area=[
    app_commands.Choice(name="Academy",    value="Academy"),
    app_commands.Choice(name="Sixth Form", value="Sixth Form"),
])
@app_commands.autocomplete(position=position_autocomplete)
async def hire(
    interaction: discord.Interaction,
    teaching_name:  str,
    staff_username: str,
    area:           str,
    position:       str,
):
    await interaction.response.defer()

    params = {
        "action":        "hire",
        "teachingName":  teaching_name,
        "staffUsername": staff_username,
        "area":          area,
        "position":      position,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(APPS_SCRIPT_URL, params=params, allow_redirects=True) as resp:
                response_text = await resp.text()
                status        = resp.status

        if status == 200 and "error" not in response_text.lower():
            embed = discord.Embed(
                title="✅ New Staff Member Hired!",
                color=discord.Color.green()
            )
            embed.add_field(name="Teaching Name",  value=teaching_name,  inline=True)
            embed.add_field(name="Staff Username", value=staff_username, inline=True)
            embed.add_field(name="Area",           value=area,           inline=True)
            embed.add_field(name="Position",       value=position,       inline=True)
            embed.set_footer(text="Edit Staff sheet has been updated ✔")
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(
                f"⚠️ Apps Script returned an unexpected response (HTTP {status}):\n"
                f"```{response_text[:500]}```\n"
                f"Check your Apps Script logs for details."
            )

    except aiohttp.ClientError as e:
        await interaction.followup.send(f"❌ Could not reach the Apps Script web app: {e}")
    except Exception as e:
        await interaction.followup.send(f"❌ Unexpected error during hire: {e}")


# ─────────────────────────────────────────────
#  /info – Staff information
# ─────────────────────────────────────────────
@bot.tree.command(name="info", description="Get information about a staff member")
@app_commands.autocomplete(staff_name=staff_autocomplete)
async def staff_info(interaction: discord.Interaction, staff_name: str):
    await interaction.response.defer()

    try:
        worksheet = spreadsheet.worksheet(ALL_STAFF_SHEET)
        all_data  = worksheet.get_all_values()

        staff_row_idx  = None
        staff_row_data = None

        for i, row in enumerate(all_data[ALL_STAFF_DATA_START - 1:], start=ALL_STAFF_DATA_START):
            if len(row) > ALL_STAFF_NAME_COL and row[ALL_STAFF_NAME_COL].strip().lower() == staff_name.lower():
                staff_row_idx  = i
                staff_row_data = row
                break

        if staff_row_data is None:
            await interaction.followup.send(f"❌ Staff member **{staff_name}** not found in '{ALL_STAFF_SHEET}'.")
            return

        embed = discord.Embed(
            title=f"📋 Staff Information: {staff_name}",
            color=discord.Color.blue()
        )

        embed.add_field(name="Position",            value=safe_get(staff_row_data, 1),      inline=True)
        embed.add_field(name="Staff Username",      value=safe_get(staff_row_data, 2),      inline=True)
        embed.add_field(name="Area",                value=safe_get(staff_row_data, 4),      inline=True)
        embed.add_field(name="TL (Training Level)", value=safe_get(staff_row_data, 5, "0"), inline=True)
        embed.add_field(name="LOA",                 value=safe_get(staff_row_data, 6),      inline=True)
        embed.add_field(name="Notes",               value=safe_get(staff_row_data, 7),      inline=True)
        embed.add_field(name="Strikes",             value=safe_get(staff_row_data, 8, "0"), inline=True)

        loa = safe_get(staff_row_data, 6)
        if loa != "N/A":
            embed.add_field(name="Leave Date",          value=safe_get(staff_row_data, 10), inline=True)
            embed.add_field(name="Reason of Departure", value=safe_get(staff_row_data, 11), inline=True)

        # ── Attendance ─────────────────────────────────────────────────
        try:
            att_sheet = spreadsheet.worksheet(ATTENDANCE_SHEET)
            att_data  = att_sheet.get_all_values()

            attendance_count  = 0
            total_sessions    = 0
            found_in_register = False

            for att_row in att_data[ATTEND_DATA_START - 1:]:
                if len(att_row) <= ATTEND_NAME_COL:
                    continue
                if att_row[ATTEND_NAME_COL].strip().lower() != staff_name.lower():
                    continue

                found_in_register = True
                for col_idx in range(ATTEND_CHECK_START, min(ATTEND_CHECK_END, len(att_row))):
                    total_sessions += 1
                    if is_checked(att_row[col_idx]):
                        attendance_count += 1
                break

            if found_in_register:
                embed.add_field(name="📅 Attendance", value=f"{attendance_count}/{total_sessions} sessions", inline=True)
            else:
                embed.add_field(name="📅 Attendance", value="Not found in attendance register", inline=True)

        except gspread.WorksheetNotFound:
            embed.add_field(name="📅 Attendance", value=f"Sheet '{ATTENDANCE_SHEET}' not found", inline=True)
        except Exception as e:
            embed.add_field(name="📅 Attendance", value=f"⚠️ Error: {e}", inline=True)

        embed.set_footer(text=f"Row {staff_row_idx} | {ALL_STAFF_SHEET}")
        await interaction.followup.send(embed=embed)

    except gspread.WorksheetNotFound:
        await interaction.followup.send(f"❌ Sheet '{ALL_STAFF_SHEET}' not found!")
    except Exception as e:
        await interaction.followup.send(f"❌ Unexpected error: {e}")


# ─────────────────────────────────────────────
#  /readsheet
# ─────────────────────────────────────────────
@bot.tree.command(name="readsheet", description="Read data from a Google Sheet")
async def read_sheet(interaction: discord.Interaction, sheet_name: str = "Sheet1"):
    try:
        await interaction.response.defer()
        worksheet = spreadsheet.worksheet(sheet_name)
        data      = worksheet.get_all_values()

        if not data:
            await interaction.followup.send("The sheet is empty!")
            return

        response = f"**Data from '{sheet_name}':**\n```\n"
        for row in data[:10]:
            response += " | ".join(str(cell) for cell in row) + "\n"
        response += "```"
        if len(data) > 10:
            response += f"\n*Showing first 10 of {len(data)} rows*"

        await interaction.followup.send(response)

    except gspread.WorksheetNotFound:
        await interaction.followup.send(f"❌ Sheet '{sheet_name}' not found!")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


# ─────────────────────────────────────────────
#  /getcell
# ─────────────────────────────────────────────
@bot.tree.command(name="getcell", description="Get value from a specific cell (e.g. A1)")
async def get_cell(interaction: discord.Interaction, cell: str, sheet_name: str = "Sheet1"):
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
        value     = worksheet.acell(cell).value
        if value:
            await interaction.response.send_message(f"**{cell}:** {value}")
        else:
            await interaction.response.send_message(f"Cell {cell} is empty")
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}")


# ─────────────────────────────────────────────
#  /search
# ─────────────────────────────────────────────
@bot.tree.command(name="search", description="Search for a value in the spreadsheet")
async def search(interaction: discord.Interaction, query: str, sheet_name: str = "Sheet1"):
    try:
        await interaction.response.defer()
        worksheet = spreadsheet.worksheet(sheet_name)
        data      = worksheet.get_all_values()

        results = []
        for row_idx, row in enumerate(data, start=1):
            for col_idx, cell in enumerate(row, start=1):
                if query.lower() in str(cell).lower():
                    col_letter = gspread.utils.rowcol_to_a1(row_idx, col_idx).rstrip('0123456789')
                    results.append(f"Row {row_idx}, Col {col_letter}: {cell}")

        if results:
            response = f"**Found {len(results)} result(s) for '{query}':**\n" + "\n".join(results[:10])
            if len(results) > 10:
                response += f"\n*Showing first 10 of {len(results)} results*"
        else:
            response = f"No results found for '{query}'"

        await interaction.followup.send(response)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


# ─────────────────────────────────────────────
#  /getrow
# ─────────────────────────────────────────────
@bot.tree.command(name="getrow", description="Get all values from a specific row")
async def get_row(interaction: discord.Interaction, row_number: int, sheet_name: str = "Sheet1"):
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
        row_data  = worksheet.row_values(row_number)
        if row_data:
            response = f"**Row {row_number}:**\n```\n" + " | ".join(row_data) + "\n```"
        else:
            response = f"Row {row_number} is empty or doesn't exist"
        await interaction.response.send_message(response)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}")


# ─────────────────────────────────────────────
#  /ping  /pong
# ─────────────────────────────────────────────
@bot.tree.command(name="ping", description="Check latency")
async def slash_ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f'Pong! 🏓 Latency: {latency}ms')

@bot.tree.command(name="pong", description="Responds with Ping")
async def slash_pong(interaction: discord.Interaction):
    await interaction.response.send_message('Ping! 🏓')


# ─────────────────────────────────────────────
#  TEXT COMMAND FALLBACK
# ─────────────────────────────────────────────
@bot.command(name='readsheet')
async def text_readsheet(ctx, sheet_name: str = "Sheet1"):
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
        data      = worksheet.get_all_values()

        if not data:
            await ctx.send("The sheet is empty!")
            return

        response = f"**Data from '{sheet_name}':**\n```\n"
        for row in data[:10]:
            response += " | ".join(str(cell) for cell in row) + "\n"
        response += "```"
        if len(data) > 10:
            response += f"\n*Showing first 10 of {len(data)} rows*"

        await ctx.send(response)
    except Exception as e:
        await ctx.send(f"❌ Error: {e}")


# ─────────────────────────────────────────────
#  RUN
# ─────────────────────────────────────────────
if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)