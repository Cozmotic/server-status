import asyncio
import json
from datetime import datetime, timedelta
import threading
import discord
from discord.ext import commands, tasks
from discord.ui import View
import requests
import os


# Shared state for all bots
player_counts = {}
lfg_last_time = None
lfg_lock = threading.Lock()


class ServerBot:
    """Class to manage a Discord bot instance for server status monitoring."""
    
    def __init__(self, bot_id, token, server_id, enable_lfg=False):
        """
        Initialize a ServerBot instance.
        
        Args:
            bot_id: Identifier for this bot (e.g., "id1", "id2")
            token: Discord bot token
            server_id: SCP server ID to monitor
            enable_lfg: Whether this bot has LFG functionality enabled
        """
        self.bot_id = bot_id
        self.token = token
        self.server_id = server_id
        self.enable_lfg = enable_lfg
        
        # Configuration
        self.refresh = 90
        self.lfg_cooldown_minutes = 60
        
        # LFG Configuration
        self.lfg_channel_id = 1419213517260853350
        self.lfg_role_id = 1419213574206918656
        self.mc_lfg_role_id = 1479916696226758707
        
        # State
        self.lfg_posts = {}
        self.current_player_count = "0/0"
        
        # Initialize Discord bot
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.presences = True
        
        self.client = commands.Bot(command_prefix="!", intents=intents)
        self._setup_commands()
        self._setup_events()
    
    def _setup_commands(self):
        """Setup bot commands."""
        if not self.enable_lfg:
            return
        
        @self.client.tree.command(name="lfg")
        async def lfg(interaction: discord.Interaction):
            global lfg_last_time
            
            try:
                await interaction.response.defer(ephemeral=True)
                
                now = datetime.now()
                with lfg_lock:
                    if lfg_last_time:
                        elapsed = (now - lfg_last_time).total_seconds() / 60
                        if elapsed < self.lfg_cooldown_minutes:
                            await interaction.followup.send("Cooldown active.", ephemeral=True)
                            return
                    lfg_last_time = now

                channel = self.client.get_channel(self.lfg_channel_id)
                if channel is None:
                    await interaction.followup.send("LFG channel not available.", ephemeral=True)
                    return

                content = self.build_lfg_content(interaction.user.id)
                msg = await channel.send(content)

                self.lfg_posts[str(interaction.user.id)] = {
                    "channel_id": channel.id,
                    "message_id": msg.id,
                    "type": "lfg",
                    "author_id": interaction.user.id
                }

                await interaction.followup.send("LFG posted.", ephemeral=True)
            
            except Exception as e:
                print(f"[{self.bot_id}] Error in /lfg command: {e}")
                try:
                    await interaction.followup.send(f"Error: {str(e)}", ephemeral=True)
                except:
                    pass
    
    def build_player_count_display(self):
        """Return a consistent player count display for all bots."""
        if player_counts:
            return "\n".join(
                [f"**{bot_id}**: {player_counts.get(bot_id, '0/0')}" for bot_id in sorted(player_counts.keys())]
            )
        return self.current_player_count

    def build_lfg_content(self, author_id=None):
        """Return the standardized LFG message content."""
        author_line = f"Posted by <@{author_id}>\n" if author_id else ""
        return f"<@&{self.lfg_role_id}>\n{author_line}{self.build_player_count_display()}"

    def _setup_events(self):
        """Setup bot events."""
        @self.client.event
        async def on_ready():
            print(f"[{self.bot_id}] {self.client.user} online")
            asyncio.create_task(self.update_server_status())
            
            try:
                await self.client.tree.sync()
            except Exception as e:
                print(f"[{self.bot_id}] Error syncing commands: {e}")
    
    async def update_server_status(self):
        """Update server status and manage LFG messages."""
        while True:
            try:
                url = f'https://api.scplist.kr/api/servers/{self.server_id}'
                resp = requests.get(url)
                
                if resp.status_code != 200:
                    await asyncio.sleep(self.refresh)
                    continue
                
                data = resp.json()
                player_count = data['players']
                
                sl_pc = int(player_count.split('/')[0])
                max_pc = int(player_count.split('/')[1])
                
                self.current_player_count = player_count
                player_counts[self.bot_id] = player_count
                
                # Update presence
                if sl_pc == 0:
                    status = discord.Status.idle
                elif sl_pc >= max_pc:
                    status = discord.Status.dnd
                else:
                    status = discord.Status.online
                
                await self.client.change_presence(
                    activity=discord.CustomActivity(name=f"Online: {player_count}"),
                    status=status
                )
                
                # Update LFG messages
                if self.lfg_posts:
                    player_count_display = self.build_player_count_display()
                    
                    for uid, info in list(self.lfg_posts.items()):
                        try:
                            channel = self.client.get_channel(info["channel_id"])
                            msg = await channel.fetch_message(info["message_id"])
                            
                            if info["type"] == "minecraft":
                                content = f"<@&{self.mc_lfg_role_id}> (Posted by <@{uid}>)"
                            else:
                                author_id = info.get("author_id")
                                author_line = f"Posted by <@{author_id}>\n" if author_id else ""
                                content = f"<@&{self.lfg_role_id}>\n{author_line}{player_count_display}"
                            
                            await msg.edit(content=content)
                        
                        except:
                            self.lfg_posts.pop(uid, None)
                
                await asyncio.sleep(self.refresh)
            
            except Exception as e:
                print(f"[{self.bot_id}] Error in update_server_status: {e}")
                await asyncio.sleep(self.refresh)
    
    def run(self):
        """Start the bot."""
        self.client.run(self.token)


async def run_all_bots():
    """Run all bot instances concurrently."""
    # Define bots: (bot_id, token_env_var, server_id, enable_lfg)
    bots_config = [
        ("Server", "THEATORS_BOT_TOKEN_2", "101529", True),
    ]
    
    bots = []
    for bot_id, token_env, server_id, enable_lfg in bots_config:
        token = os.getenv(token_env)
        if not token:
            print(f"Warning: {token_env} not found")
            continue
        
        bot = ServerBot(bot_id, token, server_id, enable_lfg)
        bots.append(bot)
    
    # Run all bots concurrently
    tasks = [asyncio.to_thread(bot.run) for bot in bots]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(run_all_bots())