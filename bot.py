import asyncio
import json
from datetime import datetime, timedelta
import discord
from discord.ui import View
from discord.ext import tasks
import requests
import os

# Initialize Discord client with required intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True
client = discord.Client(intents=intents)

# Server status constants
id2 = "95338"
refresh = 30

# Punchcard constants
PUNCHCARD_CHANNEL_ID = 1433624015557365800  # Channel for the punchcard button
PUNCHCARD_LOGS_CHANNEL_ID = 1433621369970495589  # Channel for logs and reports (Replace with your channel ID)
RESET_DAY = 0  # Monday = 0, Sunday = 6
RESET_HOUR = 5  # 24-hour format (0-23)
RESET_MINUTE = 0  # Minute (0-59)
PUNCHCARD_FILE = "punchcard_data.json"

# Data structure
punchcard_data = {}

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
            await interaction.response.send_message(f"{interaction.user.mention} punched in!", ephemeral=True)
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
                    f"{interaction.user.mention} punched out! Session time: {session_time}",
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
                await interaction.response.send_message(f"{interaction.user.mention} punched in!", ephemeral=True)
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
            report = "**Weekly Punchcard Report**\n━━━━━━━━━━━━━━━━━━\n"
            for user_id, data in punchcard_data.items():
                user = await client.fetch_user(int(user_id))
                total_hours = data["total_time"] / 3600
                report += f"**{user.display_name}** ({user.name}): `{total_hours:.2f} hours`\n"

            await channel.send(report)

            # Reset data
            punchcard_data.clear()
            save_punchcard_data()

async def update_server_status():
    last_reminder_time = {}  # Track last reminder time for each user
    reminder_cooldown = 600  # 10 minutes cooldown between reminders
    
    while True:
        s = requests.get(f'https://api.scplist.kr/api/servers/{id2}').text
        data = json.loads(s)

        player_count = data['players']
        sl_pc = int(player_count.split('/')[0])
        
        # Update bot status
        if sl_pc == 0:
            await client.change_presence(activity=discord.CustomActivity(name=f"Online: {player_count}"),
                                    status=discord.Status.idle)
            
            # Check for punched in users when server is empty
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
                                # Send log message
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
                                    # If DM fails, send ephemeral message in logs channel
                                    try:
                                        message = await logs_channel.send(f"<@{user_id}>")
                                        await message.delete()  # Delete the ping message immediately
                                        await logs_channel.send(
                                            f"You are currently punched in but the server appears to be empty! "
                                            f"Please punch out if you're not actively playing.",
                                            ephemeral=True,
                                            reference=message
                                        )
                                    except Exception as e:
                                        print(f"Failed to send ephemeral message: {e}")
                                
                                last_reminder_time[user_id] = current_time
                            except discord.NotFound:
                                print(f"Could not find user with ID {user_id}")
                            
        elif sl_pc >= int(player_count.split('/')[1]):
            await client.change_presence(activity=discord.CustomActivity(name=f"Online: {player_count}"),
                                    status=discord.Status.dnd)
        else:
            await client.change_presence(activity=discord.CustomActivity(name=f"Online: {player_count}"),
                                    status=discord.Status.online)

        await asyncio.sleep(refresh)

@client.event
async def on_ready():
    global punchcard_data
    print(f"{client.user} is now online!")
    
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
        await channel.send("Click the button below to punch in/out:", view=view)

    # Start server status update loop
    asyncio.create_task(update_server_status())

client.run(TOKEN)

