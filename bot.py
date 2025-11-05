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

# Switch to using Bot instead of Client
client = commands.Bot(command_prefix="!", intents=intents)

# Server status constants
id2 = "95631"
refresh = 90

# Punchcard constants
PUNCHCARD_CHANNEL_ID = 1433624015557365800  # Channel for the punchcard button
PUNCHCARD_LOGS_CHANNEL_ID = 1433621369970495589  # Channel for logs and reports (Replace with your channel ID)
RESET_DAY = 0  # Monday = 0, Sunday = 6
RESET_HOUR = 5  # 24-hour format (0-23)
RESET_MINUTE = 0  # Minute (0-59)
PUNCHCARD_FILE = "punchcard_data.json"

# Data structure
punchcard_data = {}

# LFG feature constants
LFG_CHANNEL_ID = 1419213517260853350  # Channel where LFG pings should be posted (replace)
LFG_ROLE_ID = 1419213574206918656  # Role to mention for LFG (replace)
LFG_COOLDOWN_MINUTES = 60  # default per-user cooldown in minutes

# Runtime LFG state
lfg_posts = {}  # user_id -> {"channel_id": int, "message_id": int}
# Global timestamp of the last /lfg (global cooldown, not per-user)
last_lfg_time = None  # datetime of last /lfg

# Current server player_count string (updated by update_server_status)
current_player_count = "0/0"

TOKEN = os.getenv("THEATORS_BOT_TOKEN")

def load_punchcard_data():
    if os.path.exists(PUNCHCARD_FILE):
        with open(PUNCHCARD_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_punchcard_data():
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
            if logs_channel:
                await logs_channel.send(f"**Punch In:** {interaction.user.display_name} ({interaction.user.name})")
        else:
            user_data = punchcard_data[user_id]
            if user_data["punched_in"]:
                # Punch out
                last_punch = datetime.fromisoformat(user_data["last_punch"])
                time_diff = (current_time - last_punch).total_seconds()
                user_data["total_time"] += time_diff
                user_data["punched_in"] = False
                session_time = timedelta(seconds=int(time_diff))
                await interaction.response.send_message(
                    f"{interaction.user.mention} punched out. Session time: {session_time}",
                    ephemeral=True
                )
                if logs_channel:
                    await logs_channel.send(
                        f"**Punch Out:** {interaction.user.display_name} ({interaction.user.name})\n"
                        f"Session duration: {session_time}"
                    )
            else:
                # Punch in
                user_data["punched_in"] = True
                user_data["last_punch"] = current_time.isoformat()
                await interaction.response.send_message(f"{interaction.user.mention} punched in.", ephemeral=True)
                if logs_channel:
                    await logs_channel.send(f"**Punch In:** {interaction.user.display_name} ({interaction.user.name})")

        save_punchcard_data()

@tasks.loop(minutes=1)
async def check_weekly_reset():
    current_time = datetime.now()
    if (current_time.weekday() == RESET_DAY and 
        current_time.hour == RESET_HOUR and 
        current_time.minute == RESET_MINUTE):
        channel = client.get_channel(PUNCHCARD_LOGS_CHANNEL_ID)
        if channel:
            # Generate report
            report = "**Weekly Punchcard Report**\n━━━━━━━━━━━━━━━━━━━━━\n"
            for user_id, data in punchcard_data.items():
                user = await client.fetch_user(int(user_id))
                total_hours = data["total_time"] / 3600
                report += f"**{user.display_name}** ({user.name}): `{total_hours:.2f} hours`\n"

            await channel.send(report)

            # Reset data
            punchcard_data.clear()
            save_punchcard_data()


@client.tree.command(name="lfg", description="Post an LFG ping in the LFG channel (cooldown applies)")
async def lfg(interaction: discord.Interaction):
    """Post an LFG ping mentioning the configured role. Optional `minutes` sets cooldown for this post (overrides default for this call)."""
    user_id = str(interaction.user.id)
    now = datetime.now()
    cooldown = LFG_COOLDOWN_MINUTES

    # Check global cooldown
    global last_lfg_time
    last = last_lfg_time
    if last is not None:
        elapsed = (now - last).total_seconds() / 60.0
        if elapsed < cooldown:
            remaining = int(cooldown - elapsed + 0.5)
            await interaction.response.send_message(f"The LFG command is on global cooldown. Please wait {remaining} more minute(s) before posting another LFG.", ephemeral=True)
            return

    # Post in the designated LFG channel
    channel = client.get_channel(LFG_CHANNEL_ID)
    if channel is None:
        await interaction.response.send_message("LFG channel is not configured or the bot cannot access it.", ephemeral=True)
        return

    # Build message content and send (do not delete messages in this channel)
    content = f"<@&{LFG_ROLE_ID}> (Posted by {interaction.user.mention})\nPlayer Count: {current_player_count}"
    try:
        msg = await channel.send(content)
    except Exception as e:
        await interaction.response.send_message(f"Failed to post LFG message: {e}", ephemeral=True)
        return

    # Track the posted message so we can update it on player count changes
    lfg_posts[user_id] = {"channel_id": channel.id, "message_id": msg.id}
    # set global last lfg time
    last_lfg_time = now

    await interaction.response.send_message(f"Posted LFG in {channel.mention}. It will be updated with player count changes.", ephemeral=True)

async def update_server_status():
    last_reminder_time = {}  # Track last reminder time for each user
    reminder_cooldown = 600  # 10 minutes cooldown between reminders

    # empty_pending: set when we see the server go from non-zero -> zero
    # server_empty_confirmed: set after we observe zero for a second consecutive cycle
    empty_pending = False
    server_empty_confirmed = False
    last_sl_pc = None

    while True:
        url = f'https://api.scplist.kr/api/servers/{id2}'
        resp = requests.get(url)

        if resp.status_code != 200:
            print(f"API ERROR: HTTP {resp.status_code}")
            await asyncio.sleep(refresh)
            continue

        raw = resp.text.strip()

        if not raw:
            print("API ERROR: Empty response from server API")
            await asyncio.sleep(refresh)
            continue

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            print("API ERROR: Invalid JSON:", raw[:200])
            await asyncio.sleep(refresh)
            continue


        player_count = data['players']
        sl_pc = int(player_count.split('/')[0])
        # update global current player_count used by LFG messages
        global current_player_count
        current_player_count = player_count

        # Update bot status
        if sl_pc == 0:
            await client.change_presence(activity=discord.CustomActivity(name=f"Online: {player_count}"),
                                        status=discord.Status.idle)

            # Determine empty confirmation state:
            # - If we just transitioned from non-zero to zero, mark pending and skip notifications this cycle
            # - If we were already pending and still zero, confirm and send notifications
            if last_sl_pc is None:
                # First run: don't immediately notify, wait one cycle to confirm
                empty_pending = True
                server_empty_confirmed = False
            elif last_sl_pc > 0 and sl_pc == 0:
                empty_pending = True
                server_empty_confirmed = False
            elif last_sl_pc == 0 and empty_pending:
                # Confirmed empty for a second cycle -> allow notifications
                server_empty_confirmed = True
                empty_pending = False

            # Only send notifications if the empty state has been confirmed at least once
            if server_empty_confirmed:
                current_time = datetime.now()
                logs_channel = client.get_channel(PUNCHCARD_LOGS_CHANNEL_ID)

                if logs_channel:
                    for user_id, data in punchcard_data.items():
                        if data["punched_in"]:
                            # Check if enough time has passed since last reminder
                            if (user_id not in last_reminder_time or 
                                (current_time - last_reminder_time[user_id]).total_seconds() >= reminder_cooldown):
                                try:
                                    user = await client.fetch_user(int(user_id))
                                    # Send log message (quiet)
                                    await logs_channel.send(
                                        f"Logged: {user.display_name} ({user.name}) was punched in while server was empty"
                                    )

                                    # Try to send DM first
                                    try:
                                        await user.send(
                                            "**Reminder:** You are currently punched in but the server appears to be empty! "
                                            "Please punch out if you're not actively playing."
                                        )
                                    except discord.Forbidden:
                                        # If DM fails, attempt a short server-only notify and an ephemeral message
                                        try:
                                            # Send a mention and delete it immediately to generate a notification
                                            ping_msg = await logs_channel.send(f"<@{user_id}>")
                                            await ping_msg.delete()
                                            # Fallback ephemeral-like message (note: ephemeral param works on interactions only)
                                            await logs_channel.send(
                                                f"You are currently punched in but the server appears to be empty! "
                                                f"Please punch out if you're not actively playing."
                                            )
                                        except Exception as e:
                                            print(f"Failed fallback notify for user {user_id}: {e}")

                                    last_reminder_time[user_id] = current_time
                                except discord.NotFound:
                                    print(f"Could not find user with ID {user_id}")

                # Update any active LFG posts with the new player count
                if lfg_posts:
                    for uid, info in list(lfg_posts.items()):
                        try:
                            channel = client.get_channel(info.get("channel_id"))
                            if channel is None:
                                continue
                            try:
                                msg = await channel.fetch_message(info.get("message_id"))
                            except discord.NotFound:
                                # message was deleted — remove tracking
                                lfg_posts.pop(uid, None)
                                continue

                            new_content = f"<@&{LFG_ROLE_ID}> (Posted by <@{uid}>)\nPlayer Count: {current_player_count}"
                            await msg.edit(content=new_content)
                        except Exception as e:
                            print(f"Failed to update LFG message for {uid}: {e}")

        elif sl_pc >= int(player_count.split('/')[1]):
            await client.change_presence(activity=discord.CustomActivity(name=f"Online: {player_count}"),
                                    status=discord.Status.dnd)
            # Reset empty state flags when players are present
            empty_pending = False
            server_empty_confirmed = False
        else:
            await client.change_presence(activity=discord.CustomActivity(name=f"Online: {player_count}"),
                                    status=discord.Status.online)
            # If there are some players but not full, treat as non-empty
            empty_pending = False
            server_empty_confirmed = False

        last_sl_pc = sl_pc

        await asyncio.sleep(refresh)

@client.event
async def on_ready():
    # Sync the application commands
    try:
        await client.tree.sync()  # This syncs the commands with Discord
        print("Slash commands synced!")
    except Exception as e:
        print(f"Failed to sync application commands: {e}")

    global punchcard_data
    print(f"{client.user} is now online")
    
    # Load existing punchcard data
    punchcard_data = load_punchcard_data()
    
    # Start the weekly reset check
    check_weekly_reset.start()
    
    # Create the punchcard message
    channel = client.get_channel(PUNCHCARD_CHANNEL_ID)
    if channel:
        # Clear existing messages in the channel
        async for message in channel.history(limit=100):
            await message.delete()
        
        # Create new punchcard message with button
        view = View(timeout=None)
        view.add_item(PunchcardButton())
        await channel.send("Click the button below to punch in/out\nIf you miss a punch, contact an O5-Council member", view=view)

    # Start server status update loop
    asyncio.create_task(update_server_status())
    # Sync application (slash) commands
    try:
        await client.tree.sync()
    except Exception as e:
        print(f"Failed to sync application commands: {e}")

# Run the bot
client.run(TOKEN)