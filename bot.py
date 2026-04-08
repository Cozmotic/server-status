import asyncio
import json
from datetime import datetime, timedelta
import discord
from discord.ext import commands, tasks
from discord.ui import View
import requests
import os

# Initialize Bot with required intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

client = commands.Bot(command_prefix="!", intents=intents)

# Server status constants
id2 = "95631"
refresh = 90

# ✅ NEW: Player logging
PLAYER_LOG_FILE = "player_log.json"
MAX_LOG_ENTRIES = 10000

def log_player_count(player_count):
    entry = {
        "timestamp": datetime.now().isoformat(),
        "player_count": player_count
    }

    if os.path.exists(PLAYER_LOG_FILE):
        try:
            with open(PLAYER_LOG_FILE, "r") as f:
                data = json.load(f)
        except:
            data = []
    else:
        data = []

    data.append(entry)

    # Limit file size
    data = data[-MAX_LOG_ENTRIES:]

    with open(PLAYER_LOG_FILE, "w") as f:
        json.dump(data, f, indent=4)

# Punchcard constants
PUNCHCARD_CHANNEL_ID = 1433624015557365800
PUNCHCARD_LOGS_CHANNEL_ID = 1433621369970495589
RESET_DAY = 0
RESET_HOUR = 10
RESET_MINUTE = 0
PUNCHCARD_FILE = "punchcard_data.json"

punchcard_data = {}

PUNCHCARD_ENABLED = False
MC_LFG_ENABLED = True

LFG_CHANNEL_ID = 1419213517260853350
LFG_ROLE_ID = 1419213574206918656
MC_LFG_ROLE_ID = 1479916696226758707
LFG_COOLDOWN_MINUTES = 60
MC_LFG_COOLDOWN_MINUTES = 60

lfg_posts = {}
last_lfg_time = None

current_player_count = "0/0"

TOKEN = os.getenv("THEATORS_BOT_TOKEN")

def load_punchcard_data():
    if not PUNCHCARD_ENABLED:
        return {}
    if os.path.exists(PUNCHCARD_FILE):
        with open(PUNCHCARD_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_punchcard_data():
    if not PUNCHCARD_ENABLED:
        return
    with open(PUNCHCARD_FILE, 'w') as f:
        json.dump(punchcard_data, f, indent=4)

class PunchcardButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Punch",
            style=discord.ButtonStyle.primary,
            custom_id="punchcard_button"
        )

    async def callback(self, interaction: discord.Interaction):
        if not PUNCHCARD_ENABLED:
            await interaction.response.send_message("Punchcard functionality is disabled.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        current_time = datetime.now()
        logs_channel = interaction.client.get_channel(PUNCHCARD_LOGS_CHANNEL_ID)

        if user_id not in punchcard_data:
            punchcard_data[user_id] = {
                "punched_in": True,
                "last_punch": current_time.isoformat(),
                "total_time": 0
            }
            await interaction.response.send_message(f"{interaction.user.mention} punched in.", ephemeral=True)
        else:
            user_data = punchcard_data[user_id]
            if user_data["punched_in"]:
                last_punch = datetime.fromisoformat(user_data["last_punch"])
                time_diff = (current_time - last_punch).total_seconds()
                user_data["total_time"] += time_diff
                user_data["punched_in"] = False
                await interaction.response.send_message(f"{interaction.user.mention} punched out.", ephemeral=True)
            else:
                user_data["punched_in"] = True
                user_data["last_punch"] = current_time.isoformat()
                await interaction.response.send_message(f"{interaction.user.mention} punched in.", ephemeral=True)

        save_punchcard_data()

@tasks.loop(minutes=1)
async def check_weekly_reset():
    if not PUNCHCARD_ENABLED:
        return

    current_time = datetime.now()
    if (current_time.weekday() == RESET_DAY and 
        current_time.hour == RESET_HOUR and 
        current_time.minute == RESET_MINUTE):
        channel = client.get_channel(PUNCHCARD_LOGS_CHANNEL_ID)
        if channel:
            report = "**Weekly Punchcard Report**\n━━━━━━━━━━━━━━━━━━━━━\n"
            for user_id, data in punchcard_data.items():
                user = await client.fetch_user(int(user_id))
                total_hours = data["total_time"] / 3600
                report += f"**{user.display_name}**: `{total_hours:.2f} hours`\n"

            await channel.send(report)
            punchcard_data.clear()
            save_punchcard_data()

@client.tree.command(name="lfg")
async def lfg(interaction: discord.Interaction):
    global last_lfg_time

    now = datetime.now()
    if last_lfg_time:
        elapsed = (now - last_lfg_time).total_seconds() / 60
        if elapsed < LFG_COOLDOWN_MINUTES:
            await interaction.response.send_message("Cooldown active.", ephemeral=True)
            return

    channel = client.get_channel(LFG_CHANNEL_ID)
    msg = await channel.send(f"<@&{LFG_ROLE_ID}> (Posted by {interaction.user.mention})\nPlayer Count: {current_player_count}")

    lfg_posts[str(interaction.user.id)] = {
        "channel_id": channel.id,
        "message_id": msg.id,
        "type": "lfg"
    }

    last_lfg_time = now
    await interaction.response.send_message("LFG posted.", ephemeral=True)

async def update_server_status():
    last_sl_pc = None
    last_player_count = None

    while True:
        url = f'https://api.scplist.kr/api/servers/{id2}'
        resp = requests.get(url)

        if resp.status_code != 200:
            await asyncio.sleep(refresh)
            continue

        data = resp.json()
        player_count = data['players']

        sl_pc = int(player_count.split('/')[0])
        max_pc = int(player_count.split('/')[1])

        global current_player_count
        current_player_count = player_count

        # ✅ LOG ONLY IF CHANGED
        if player_count != last_player_count:
            log_player_count(player_count)
            last_player_count = player_count

        # Presence
        if sl_pc == 0:
            status = discord.Status.idle
        elif sl_pc >= max_pc:
            status = discord.Status.dnd
        else:
            status = discord.Status.online

        await client.change_presence(
            activity=discord.CustomActivity(name=f"Online: {player_count}"),
            status=status
        )

        # Update LFG messages
        for uid, info in list(lfg_posts.items()):
            try:
                channel = client.get_channel(info["channel_id"])
                msg = await channel.fetch_message(info["message_id"])

                if info["type"] == "minecraft":
                    content = f"<@&{MC_LFG_ROLE_ID}> (Posted by <@{uid}>)"
                else:
                    content = f"<@&{LFG_ROLE_ID}> (Posted by <@{uid}>)\nPlayer Count: {player_count}"

                await msg.edit(content=content)

            except:
                lfg_posts.pop(uid, None)

        last_sl_pc = sl_pc
        await asyncio.sleep(refresh)

@client.event
async def on_ready():
    print(f"{client.user} online")

    if PUNCHCARD_ENABLED:
        global punchcard_data
        punchcard_data = load_punchcard_data()
        check_weekly_reset.start()

    asyncio.create_task(update_server_status())

    try:
        await client.tree.sync()
    except Exception as e:
        print(e)

client.run(TOKEN)