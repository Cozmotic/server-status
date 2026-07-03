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

        # Flagged-role configuration: set to a role ID to watch for. When a
        # member is found holding this role (on join, on role update, or
        # during the startup scan), staff are notified in the staff report
        # channel instead of the member being kicked. Leave 0 to disable.
        self.auto_kick_role_id = 1522404077584122017
        # Members holding any of these roles never trigger an alert, even if
        # they hold auto_kick_role_id.
        self.auto_kick_exempt_role_ids = [1419382592301564116, 1419331031466643639]

        # Role to ping in the staff report channel when a member is flagged.
        # Leave 0 to post the alert without pinging a role.
        self.staff_ping_role_id = 0  # Replace with your staff role ID.

        # Safety cap: if a bulk scan (e.g. on startup) finds more than this
        # many flagged members at once, staff get a single summary alert
        # instead of one ping per member. This avoids spamming the staff
        # channel and signals that auto_kick_role_id might be misconfigured
        # (e.g. pointing at a role far more members hold than expected).
        self.max_flags_per_scan = 3

        # Channel where staff are notified when a member is found holding
        # the flagged role, and other moderation-relevant events.
        self.staff_report_channel_id = 782358025663021146  # Replace with your staff/log channel ID.

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

    async def _report_to_staff(self, message, ping_role=False):
        """Send a moderation-relevant message to the staff report channel.

        If ping_role is True and staff_ping_role_id is set, the role is
        pinged as part of the message. Falls back to console logging if no
        channel is configured or the send fails for any reason (missing
        perms, channel deleted, etc.).
        """
        prefix = f"<@&{self.staff_ping_role_id}> " if (ping_role and self.staff_ping_role_id) else ""
        full_message = f"{prefix}{message}"

        print(f"[{self.bot_id}] STAFF REPORT: {message}")

        if not self.staff_report_channel_id:
            return

        channel = self.client.get_channel(self.staff_report_channel_id)
        if channel is None:
            print(f"[{self.bot_id}] Staff report channel not available/cached; message above was console-only.")
            return

        try:
            await channel.send(full_message)
        except Exception as e:
            print(f"[{self.bot_id}] Failed to send staff report: {e}")

    # ------------------------------------------------------------------
    # Flagged-role detection (alerts staff instead of kicking)
    # ------------------------------------------------------------------

    def _member_has_exempt_role(self, member):
        return any(role.id in self.auto_kick_exempt_role_ids for role in member.roles)

    def _member_has_flagged_role(self, member):
        return self.auto_kick_role_id and any(role.id == self.auto_kick_role_id for role in member.roles)

    def _member_is_flagged(self, member):
        if not self._member_has_flagged_role(member):
            return False
        if self._member_has_exempt_role(member):
            return False
        return True

    async def _alert_staff_of_flagged_member(self, member):
        """Notify staff (pinging the configured staff role) that a single
        member has been found holding the flagged role. Used for individual
        events (member join, role update).

        Only the bot instance with enable_lfg=True sends these alerts, so
        that if multiple bot instances share the same staff channel, staff
        aren't pinged more than once for the same member."""
        if not self.enable_lfg:
            return

        if not self._member_has_flagged_role(member):
            return

        if self._member_has_exempt_role(member):
            print(f"[{self.bot_id}] Skipping alert for {member}; exempt role present")
            return

        await self._report_to_staff(
            f"{member.mention} has just been given the "
            f"<@&{self.auto_kick_role_id}> role in {member.guild.name}. Please review.",
            ping_role=True
        )

    async def _scan_guilds_for_forbidden_role(self):
        """Bulk scan run on startup. If max_flags_per_scan or fewer members
        are found holding the flagged role, staff get one ping per member.
        If more than that are found at once, a single summary alert is sent
        instead of pinging once per member — this also acts as a signal that
        auto_kick_role_id may be misconfigured.

        Only the bot instance with enable_lfg=True runs this scan, so that
        if multiple bot instances share the same staff channel, staff aren't
        alerted twice for the same members."""
        if not self.enable_lfg:
            return

        if not self.auto_kick_role_id:
            return

        for guild in self.client.guilds:
            # Make sure the member cache is actually complete before scanning.
            try:
                if guild.chunked is False:
                    await guild.chunk()
            except Exception as e:
                print(f"[{self.bot_id}] Could not chunk members for {guild.name}: {e}")

            flagged = [m for m in guild.members if self._member_is_flagged(m)]

            if len(flagged) == 0:
                continue

            if len(flagged) > self.max_flags_per_scan:
                names = ", ".join(m.mention for m in flagged[:20])
                more = f" and {len(flagged) - 20} more" if len(flagged) > 20 else ""
                await self._report_to_staff(
                    f"Startup scan in {guild.name} found {len(flagged)} members holding "
                    f"<@&{self.auto_kick_role_id}> at once, more than the per-scan summary "
                    f"threshold of {self.max_flags_per_scan}. This may mean the flagged role "
                    f"is misconfigured — please verify it. Affected members: {names}{more}",
                    ping_role=True
                )
                continue

            for member in flagged:
                await self._alert_staff_of_flagged_member(member)
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
            await self._alert_staff_of_flagged_member(member)

        @self.client.event
        async def on_member_update(before, after):
            if not self.auto_kick_role_id:
                return

            before_ids = {role.id for role in before.roles}
            after_ids = {role.id for role in after.roles}

            gained_flagged = (
                self.auto_kick_role_id in after_ids
                and self.auto_kick_role_id not in before_ids
            )

            # If a member already had the flagged role but was protected by
            # an exempt role, and that exempt role has just been removed,
            # they're now eligible for an alert and should be re-checked.
            had_exempt = any(r in before_ids for r in self.auto_kick_exempt_role_ids)
            has_exempt = any(r in after_ids for r in self.auto_kick_exempt_role_ids)
            lost_exemption = (
                self.auto_kick_role_id in after_ids
                and had_exempt
                and not has_exempt
            )

            if gained_flagged or lost_exemption:
                await self._alert_staff_of_flagged_member(after)

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
        ("Server 1", "THEATORS_BOT_TOKEN", "95631", True),
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