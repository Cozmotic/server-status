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

        # Automatic kick configuration: set to a role ID to kick any member who has that role.
        self.auto_kick_role_id = 1522404077584122017  # Replace with the role ID to auto-kick, or leave 0 to disable.
        self.auto_kick_exempt_role_ids = [1419382592301564116, 1419331031466643639]  # Members with any of these roles will not be kicked.

        # Safety cap: if a bulk scan (e.g. on startup) finds more than this many
        # kick-eligible members at once, refuse to kick any of them and report
        # to staff instead. This protects against a misconfigured role ID
        # causing a mass-kick.
        self.max_kicks_per_scan = 3

        # Channel where staff should be notified about auto-kicks, cancelled
        # bulk-kick attempts, and other moderation-relevant events. Set this
        # to a real channel ID to enable reporting.
        self.staff_report_channel_id = 0  # Replace with your staff/log channel ID.

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

    # ------------------------------------------------------------------
    # Staff reporting
    # ------------------------------------------------------------------

    async def _report_to_staff(self, message):
        """Send a moderation-relevant message to the staff report channel.

        Falls back to console logging if no channel is configured or the
        send fails for any reason (missing perms, channel deleted, etc.).
        """
        print(f"[{self.bot_id}] STAFF REPORT: {message}")

        if not self.staff_report_channel_id:
            return

        channel = self.client.get_channel(self.staff_report_channel_id)
        if channel is None:
            print(f"[{self.bot_id}] Staff report channel not available/cached; message above was console-only.")
            return

        try:
            await channel.send(message)
        except Exception as e:
            print(f"[{self.bot_id}] Failed to send staff report: {e}")

    # ------------------------------------------------------------------
    # Auto-kick logic
    # ------------------------------------------------------------------

    def _member_has_exempt_role(self, member):
        return any(role.id in self.auto_kick_exempt_role_ids for role in member.roles)

    def _member_has_auto_kick_role(self, member):
        return self.auto_kick_role_id and any(role.id == self.auto_kick_role_id for role in member.roles)

    def _member_is_privileged(self, member):
        """Members we should never auto-kick regardless of role config."""
        guild = member.guild
        if guild.owner_id == member.id:
            return True
        try:
            if member.guild_permissions.administrator:
                return True
        except AttributeError:
            pass
        return False

    def _member_is_kick_eligible(self, member):
        if not self._member_has_auto_kick_role(member):
            return False
        if self._member_has_exempt_role(member):
            return False
        if self._member_is_privileged(member):
            return False
        return True

    async def _kick_member_if_forbidden(self, member):
        """Kick a single member if eligible. Used for individual events
        (member join, role update) where at most one member is ever in
        scope, so no bulk-safety cap is needed here."""
        if not self._member_has_auto_kick_role(member):
            return

        if self._member_has_exempt_role(member):
            print(f"[{self.bot_id}] Skipping kick for {member}; exempt role present")
            return

        if self._member_is_privileged(member):
            await self._report_to_staff(
                f":warning: {member} ({member.id}) has the auto-kick role but is the server owner "
                f"or an administrator. Skipped automatic kick — please review manually."
            )
            return

        try:
            await member.kick(reason=f"Automatic kick for forbidden role {self.auto_kick_role_id}")
            print(f"[{self.bot_id}] Kicked member {member} for role {self.auto_kick_role_id}")
            await self._report_to_staff(
                f":boot: Auto-kicked {member} ({member.id}) for holding role {self.auto_kick_role_id}."
            )
        except Exception as e:
            print(f"[{self.bot_id}] Failed to kick member {member}: {e}")
            await self._report_to_staff(
                f":x: Failed to auto-kick {member} ({member.id}): {e}"
            )

    async def _scan_guilds_for_forbidden_role(self):
        """Bulk scan run on startup. Only ever kicks if exactly one
        member is eligible in a guild. If more than
        `max_kicks_per_scan` members are eligible, the whole sweep for
        that guild is cancelled and staff are notified instead — this
        protects against a misconfigured role ID mass-kicking members."""
        if not self.auto_kick_role_id:
            return

        for guild in self.client.guilds:
            # Make sure the member cache is actually complete before scanning.
            try:
                if guild.chunked is False:
                    await guild.chunk()
            except Exception as e:
                print(f"[{self.bot_id}] Could not chunk members for {guild.name}: {e}")

            eligible = [m for m in guild.members if self._member_is_kick_eligible(m)]
            privileged_but_flagged = [
                m for m in guild.members
                if self._member_has_auto_kick_role(m)
                and not self._member_has_exempt_role(m)
                and self._member_is_privileged(m)
            ]

            for m in privileged_but_flagged:
                await self._report_to_staff(
                    f":warning: {m} ({m.id}) in **{guild.name}** has the auto-kick role but is the "
                    f"server owner or an administrator. Skipped — please review manually."
                )

            if len(eligible) == 0:
                continue

            if len(eligible) > self.max_kicks_per_scan:
                names = ", ".join(f"{m} ({m.id})" for m in eligible[:20])
                more = f" and {len(eligible) - 20} more" if len(eligible) > 20 else ""
                await self._report_to_staff(
                    f":rotating_light: Startup auto-kick scan in **{guild.name}** found "
                    f"{len(eligible)} members eligible for kicking (limit is "
                    f"{self.max_kicks_per_scan} per scan). **No one was kicked.** "
                    f"This usually means `auto_kick_role_id` is misconfigured — please verify it "
                    f"before re-running. Affected members: {names}{more}"
                )
                continue

            # len(eligible) is within the allowed cap — safe to proceed.
            for member in eligible:
                await self._kick_member_if_forbidden(member)
                await asyncio.sleep(1)

    def _setup_events(self):
        """Setup bot events."""
        @self.client.event
        async def on_ready():
            print(f"[{self.bot_id}] {self.client.user} online")
            if self.auto_kick_role_id:
                await self._scan_guilds_for_forbidden_role()
            asyncio.create_task(self.update_server_status())

            try:
                await self.client.tree.sync()
            except Exception as e:
                print(f"[{self.bot_id}] Error syncing commands: {e}")

        @self.client.event
        async def on_member_join(member):
            await self._kick_member_if_forbidden(member)

        @self.client.event
        async def on_member_update(before, after):
            if not self.auto_kick_role_id:
                return

            before_ids = {role.id for role in before.roles}
            after_ids = {role.id for role in after.roles}

            gained_forbidden = (
                self.auto_kick_role_id in after_ids
                and self.auto_kick_role_id not in before_ids
            )

            # If a member already had the forbidden role but was protected by
            # an exempt role, and that exempt role has just been removed,
            # they are now eligible and should be re-checked.
            had_exempt = any(r in before_ids for r in self.auto_kick_exempt_role_ids)
            has_exempt = any(r in after_ids for r in self.auto_kick_exempt_role_ids)
            lost_exemption = (
                self.auto_kick_role_id in after_ids
                and had_exempt
                and not has_exempt
            )

            if gained_forbidden or lost_exemption:
                await self._kick_member_if_forbidden(after)

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
        ("Server 1", "THEATORS_BOT_TOKEN", "95631", True)
        ("Server 2", "THEATORS_BOT_TOKEN_2", "101529", False),
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