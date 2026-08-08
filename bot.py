import asyncio
import json
import uuid
from datetime import datetime, timedelta, time as dtime
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


class QOTDModal(discord.ui.Modal):
    """Modal used both to submit a brand-new QOTD suggestion and to edit
    an existing pending one (pre-filled when `existing` is provided)."""

    def __init__(self, bot_ref, existing=None):
        super().__init__(title="Edit QOTD" if existing else "Submit a QOTD")
        self.bot_ref = bot_ref
        self.existing = existing

        self.question_input = discord.ui.TextInput(
            label="Question",
            style=discord.TextStyle.short,
            max_length=200,
            default=existing["question"] if existing else None,
            required=True,
        )
        self.answers_input = discord.ui.TextInput(
            label="Answers (one per line, 2-10)",
            style=discord.TextStyle.paragraph,
            default="\n".join(existing["answers"]) if existing else None,
            required=True,
        )
        self.multiple_input = discord.ui.TextInput(
            label="Allow multiple answers? (yes/no)",
            style=discord.TextStyle.short,
            max_length=3,
            default=("yes" if existing["multiple"] else "no") if existing else "no",
            required=True,
        )

        self.add_item(self.question_input)
        self.add_item(self.answers_input)
        self.add_item(self.multiple_input)

    async def on_submit(self, interaction: discord.Interaction):
        answers = [line.strip() for line in str(self.answers_input.value).split("\n") if line.strip()]

        if len(answers) < 2:
            await interaction.response.send_message("Please provide at least 2 answers.", ephemeral=True)
            return
        if len(answers) > 10:
            await interaction.response.send_message(
                "Discord polls support a maximum of 10 answers -- please trim your list.", ephemeral=True
            )
            return

        multiple = str(self.multiple_input.value).strip().lower() in ("yes", "y", "true", "1")
        question = str(self.question_input.value).strip()

        if self.existing:
            await self.bot_ref._update_qotd_entry(interaction, self.existing["id"], question, answers, multiple)
        else:
            await self.bot_ref._create_qotd_entry(interaction, question, answers, multiple)


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
        self.refresh = 60  # Poll scplist.kr v2 players endpoint every 60s
        self.lfg_cooldown_minutes = 60

        # LFG Configuration
        self.lfg_channel_id = 1419213517260853350
        self.lfg_role_id = 1419213574206918656
        self.mc_lfg_role_id = 1479916696226758707

        # Flagged-role configuration: set to a role ID to watch for. When a
        # member is found holding this role (on join, on role update, or
        # during the startup scan), staff are notified in the staff report
        # channel instead of the member being kicked. Leave 0 to disable.
        self.auto_kick_role_id = 1522399280030154792
        # Members holding any of these roles never trigger an alert, even if
        # they hold auto_kick_role_id.
        self.auto_kick_exempt_role_ids = [1419382592301564116, 1419331031466643639]

        # Role to ping in the staff report channel when a member is flagged.
        # Leave 0 to post the alert without pinging a role.
        self.staff_ping_role_id = 1419382592301564116  # Replace with your staff role ID.

        # Channel where staff are notified when a member is found holding
        # the flagged role, and other moderation-relevant events.
        self.staff_report_channel_id = 1419155961033130016  # Replace with your staff/log channel ID.

        # Members who have already been alerted on are recorded here so
        # they are never pinged about again, even across restarts or if
        # they lose and re-gain the flagged role. Persisted to a small JSON
        # file on disk, one per bot instance.
        self.alerted_members_file = f"alerted_members_{self.bot_id}.json"
        self.alerted_member_ids = self._load_id_set(self.alerted_members_file)

        # Watched-channel message alert: when any non-bot member posts a
        # message in this channel, staff are notified -- unlike the
        # flagged-role alert above, exempt roles do NOT prevent this alert,
        # since posting in the channel is itself the signal being watched
        # for. Set to 0 to disable. The channel's first-ever message is
        # treated as a pinned info message and never triggers an alert.
        self.watched_message_channel_id = 1522658894529433710  # Set to a channel ID to enable.

        # Members who have already been alerted on for posting in the
        # watched channel, tracked separately from the flagged-role alerts
        # above (a member could trigger one, the other, or both).
        self.alerted_message_authors_file = f"alerted_message_authors_{self.bot_id}.json"
        self.alerted_message_author_ids = self._load_id_set(self.alerted_message_authors_file)

        # Cache of the watched channel's first (oldest) message ID, looked
        # up lazily the first time a message in that channel is seen.
        self._watched_channel_first_message_id = None

        # ------------------------------------------------------------------
        # QOTD (Question of the Day) configuration -- LFG bot instance only.
        # ------------------------------------------------------------------

        # Role allowed to submit QOTD suggestions and manage them (the
        # buttons on queued suggestions -- Use Today / Remove / Edit /
        # Post Now -- are restricted to this same role).
        self.qotd_manager_role_id = 1441033874518970439  # Replace with your QOTD-manager role ID.

        # Channel where newly-submitted QOTD suggestions are posted as an
        # embed with management buttons, and where they stay (buttons
        # removed) once removed or used.
        self.qotd_queue_channel_id = 1533305968639868949  # Replace with your QOTD queue/review channel ID.

        # Channel where the daily (or force-posted) native Discord poll is
        # sent, with a public thread attached.
        self.qotd_poll_channel_id = 1531845754509983934  # Replace with your QOTD polling channel ID.

        # Role pinged inside the thread created under each posted poll.
        self.qotd_ping_role_id = 1441101758846730321  # Replace with your QOTD ping role ID.

        # How long the native poll stays open for voting, in hours.
        self.qotd_poll_duration_hours = 24

        # Time of day (UTC) the daily QOTD task runs and posts a poll, if
        # one is available in the queue.
        self.qotd_post_hour_utc = 19
        self.qotd_post_minute_utc = 30

        # Persisted QOTD queue -- list of dicts, one per submitted
        # suggestion, surviving restarts. See _load_qotd_data.
        self.qotd_data_file = f"qotd_data_{self.bot_id}.json"
        self.qotd_entries = self._load_qotd_data()
        self.qotd_daily_task = None

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
        self._setup_qotd_task()

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

        @self.client.tree.command(name="qotd", description="Submit a Question of the Day suggestion")
        async def qotd(interaction: discord.Interaction):
            try:
                if not await self._check_qotd_manager(interaction):
                    return
                await interaction.response.send_modal(QOTDModal(self))
            except Exception as e:
                print(f"[{self.bot_id}] Error in /qotd command: {e}")
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(f"Error: {str(e)}", ephemeral=True)
                    else:
                        await interaction.response.send_message(f"Error: {str(e)}", ephemeral=True)
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
    # QOTD (Question of the Day)
    # ------------------------------------------------------------------

    def _load_qotd_data(self):
        """Load the persisted QOTD queue from disk."""
        try:
            with open(self.qotd_data_file, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        except Exception as e:
            print(f"[{self.bot_id}] Failed to load {self.qotd_data_file}: {e}")
            return []

    def _save_qotd_data(self):
        """Persist the QOTD queue to disk."""
        try:
            with open(self.qotd_data_file, "w") as f:
                json.dump(self.qotd_entries, f, indent=2)
        except Exception as e:
            print(f"[{self.bot_id}] Failed to save {self.qotd_data_file}: {e}")

    def _get_qotd_entry(self, qid):
        """Look up a QOTD entry by its ID."""
        for entry in self.qotd_entries:
            if entry["id"] == qid:
                return entry
        return None

    async def _check_qotd_manager(self, interaction: discord.Interaction):
        """Return True if the interacting member holds the QOTD manager
        role. Otherwise sends an ephemeral denial and returns False. Used
        to gate both the /qotd command and every button on a queued
        suggestion."""
        member = interaction.user
        has_role = isinstance(member, discord.Member) and any(
            role.id == self.qotd_manager_role_id for role in member.roles
        )
        if not has_role:
            await interaction.response.send_message(
                "You don't have permission to manage QOTD suggestions.", ephemeral=True
            )
            return False
        return True

    def _build_qotd_embed(self, entry):
        """Build the embed shown in the QOTD queue channel for one entry,
        styled according to its current status (pending / removed / used)."""
        answers_text = "\n".join(f"{i + 1}. {a}" for i, a in enumerate(entry["answers"])) or "-"
        status = entry["status"]

        if status == "removed":
            embed = discord.Embed(
                title="🗑️ QOTD Suggestion (Removed)",
                description=f"~~{entry['question']}~~",
                color=discord.Color.red(),
            )
            embed.add_field(name="Answers", value=answers_text, inline=False)
        elif status == "used":
            embed = discord.Embed(
                title="✅ QOTD (Posted)",
                description=entry["question"],
                color=discord.Color.green(),
            )
            embed.add_field(name="Answers", value=answers_text, inline=False)
        else:
            title = "📋 QOTD Suggestion"
            if entry.get("force_today"):
                title += " 📌 (Flagged for Today)"
            embed = discord.Embed(
                title=title,
                description=entry["question"],
                color=discord.Color.blurple(),
            )
            embed.add_field(name="Answers", value=answers_text, inline=False)

        embed.add_field(
            name="Multiple Answers Allowed",
            value="Yes" if entry["multiple"] else "No",
            inline=True,
        )
        embed.set_footer(text=f"ID: {entry['id']}")
        embed.add_field(name="Submitted by", value=f"<@{entry['author_id']}>", inline=True)
        return embed

    def _build_qotd_view(self, entry):
        """Build the management buttons for a queued QOTD entry. Returns
        None once the entry is no longer pending (removed/used), which
        removes the buttons from the message when re-rendered."""
        if entry["status"] != "pending":
            return None

        qid = entry["id"]
        view = discord.ui.View(timeout=None)

        async def use_today_cb(interaction: discord.Interaction):
            if not await self._check_qotd_manager(interaction):
                return
            e = self._get_qotd_entry(qid)
            if not e or e["status"] != "pending":
                await interaction.response.send_message("This suggestion is no longer pending.", ephemeral=True)
                return
            e["force_today"] = True
            self._save_qotd_data()
            await self._refresh_qotd_message(e)
            await interaction.response.send_message(
                "Flagged -- this will be prioritized in today's QOTD draw.", ephemeral=True
            )

        async def remove_cb(interaction: discord.Interaction):
            if not await self._check_qotd_manager(interaction):
                return
            e = self._get_qotd_entry(qid)
            if not e or e["status"] != "pending":
                await interaction.response.send_message("This suggestion is no longer pending.", ephemeral=True)
                return
            e["status"] = "removed"
            self._save_qotd_data()
            await self._refresh_qotd_message(e)
            await interaction.response.send_message("Suggestion removed.", ephemeral=True)

        async def edit_cb(interaction: discord.Interaction):
            if not await self._check_qotd_manager(interaction):
                return
            e = self._get_qotd_entry(qid)
            if not e or e["status"] != "pending":
                await interaction.response.send_message("This suggestion is no longer pending.", ephemeral=True)
                return
            await interaction.response.send_modal(QOTDModal(self, existing=e))

        async def post_now_cb(interaction: discord.Interaction):
            if not await self._check_qotd_manager(interaction):
                return
            e = self._get_qotd_entry(qid)
            if not e or e["status"] != "pending":
                await interaction.response.send_message("This suggestion is no longer pending.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            ok = await self._post_qotd_poll(e)
            if ok:
                await interaction.followup.send("Posted to the polling channel.", ephemeral=True)
            else:
                await interaction.followup.send(
                    "Failed to post -- check the QOTD polling channel configuration.", ephemeral=True
                )

        use_btn = discord.ui.Button(
            label="Use Today", style=discord.ButtonStyle.primary, custom_id=f"qotd:use:{qid}"
        )
        use_btn.callback = use_today_cb

        remove_btn = discord.ui.Button(
            label="Remove", style=discord.ButtonStyle.danger, custom_id=f"qotd:remove:{qid}"
        )
        remove_btn.callback = remove_cb

        edit_btn = discord.ui.Button(
            label="Edit", style=discord.ButtonStyle.secondary, custom_id=f"qotd:edit:{qid}"
        )
        edit_btn.callback = edit_cb

        post_btn = discord.ui.Button(
            label="Post Now", style=discord.ButtonStyle.success, custom_id=f"qotd:postnow:{qid}"
        )
        post_btn.callback = post_now_cb

        view.add_item(use_btn)
        view.add_item(remove_btn)
        view.add_item(edit_btn)
        view.add_item(post_btn)
        return view

    async def _refresh_qotd_message(self, entry):
        """Re-render a QOTD entry's message in the queue channel -- embed
        plus buttons (or no buttons, once it's removed/used)."""
        channel = self.client.get_channel(entry["channel_id"])
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(entry["message_id"])
        except Exception as e:
            print(f"[{self.bot_id}] Could not fetch QOTD message {entry.get('message_id')}: {e}")
            return

        embed = self._build_qotd_embed(entry)
        view = self._build_qotd_view(entry)
        try:
            await msg.edit(embed=embed, view=view)
        except Exception as e:
            print(f"[{self.bot_id}] Could not edit QOTD message: {e}")

    async def _create_qotd_entry(self, interaction: discord.Interaction, question, answers, multiple):
        """Create a new pending QOTD entry and post it to the queue channel."""
        channel = self.client.get_channel(self.qotd_queue_channel_id)
        if channel is None:
            await interaction.response.send_message("QOTD queue channel not available.", ephemeral=True)
            return

        entry = {
            "id": uuid.uuid4().hex[:8],
            "question": question,
            "answers": answers,
            "multiple": multiple,
            "status": "pending",
            "force_today": False,
            "author_id": interaction.user.id,
            "created_at": datetime.utcnow().isoformat(),
            "message_id": None,
            "channel_id": channel.id,
        }

        try:
            msg = await channel.send(embed=self._build_qotd_embed(entry), view=self._build_qotd_view(entry))
        except Exception as e:
            await interaction.response.send_message(f"Failed to post suggestion: {e}", ephemeral=True)
            return

        entry["message_id"] = msg.id
        self.qotd_entries.append(entry)
        self._save_qotd_data()
        await interaction.response.send_message("QOTD suggestion submitted.", ephemeral=True)

    async def _update_qotd_entry(self, interaction: discord.Interaction, qid, question, answers, multiple):
        """Apply edits to an existing pending QOTD entry."""
        entry = self._get_qotd_entry(qid)
        if not entry or entry["status"] != "pending":
            await interaction.response.send_message("This suggestion is no longer pending.", ephemeral=True)
            return

        entry["question"] = question
        entry["answers"] = answers
        entry["multiple"] = multiple
        self._save_qotd_data()
        await self._refresh_qotd_message(entry)
        await interaction.response.send_message("QOTD suggestion updated.", ephemeral=True)

    async def _post_qotd_poll(self, entry):
        """Post a QOTD entry as a native Discord poll in the polling
        channel, attach a public thread named 'QOTD <Mon> <Day> <Year>',
        ping the QOTD role inside it, and mark the entry as used. Returns
        True on success."""
        channel = self.client.get_channel(self.qotd_poll_channel_id)
        if channel is None:
            print(f"[{self.bot_id}] QOTD poll channel not available.")
            return False

        try:
            poll = discord.Poll(
                question=entry["question"],
                duration=timedelta(hours=self.qotd_poll_duration_hours),
                multiple=entry["multiple"],
            )
            for answer in entry["answers"]:
                poll.add_answer(text=answer)

            msg = await channel.send(poll=poll)

            now = datetime.utcnow()
            thread_name = f"QOTD {now.strftime('%b')} {now.day} {now.year}"
            thread = await msg.create_thread(name=thread_name)

            if self.qotd_ping_role_id:
                await thread.send(f"<@&{self.qotd_ping_role_id}>")

            entry["status"] = "used"
            entry["force_today"] = False
            self._save_qotd_data()
            await self._refresh_qotd_message(entry)
            return True

        except Exception as e:
            print(f"[{self.bot_id}] Error posting QOTD poll: {e}")
            return False

    async def _qotd_daily_tick(self):
        """Runs once a day. Picks a pending QOTD -- preferring any flagged
        'Use Today', otherwise the oldest pending suggestion -- and posts
        it. If none are pending, skips silently (just logs)."""
        print(f"[{self.bot_id}] QOTD daily tick fired at {datetime.utcnow().isoformat()} UTC")
        try:
            pending = [e for e in self.qotd_entries if e["status"] == "pending"]
            if not pending:
                print(f"[{self.bot_id}] No QOTD suggestions available; skipping today's post.")
                return

            flagged = [e for e in pending if e.get("force_today")]
            pool = flagged if flagged else pending
            pool.sort(key=lambda e: e["created_at"])
            chosen = pool[0]

            posted = await self._post_qotd_poll(chosen)
            print(f"[{self.bot_id}] QOTD daily tick posted question {chosen['id']}: {posted}")
        except Exception as e:
            print(f"[{self.bot_id}] Error in QOTD daily task: {e}")
        finally:
            next_run = self.qotd_daily_task.next_iteration if self.qotd_daily_task else None
            print(f"[{self.bot_id}] Next QOTD run scheduled for "
                  f"{next_run.isoformat() if next_run else 'unknown'}")

    async def _qotd_before_loop(self):
        await self.client.wait_until_ready()

    def _setup_qotd_task(self):
        """Create the once-a-day QOTD posting task. Only set up on the LFG
        bot instance."""
        if not self.enable_lfg:
            return
        self.qotd_daily_task = tasks.loop(
            time=dtime(hour=self.qotd_post_hour_utc, minute=self.qotd_post_minute_utc)
        )(self._qotd_daily_tick)
        self.qotd_daily_task.before_loop(self._qotd_before_loop)

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

    def _load_id_set(self, path):
        """Load a JSON list of IDs from disk into a set. Used for both the
        flagged-role and watched-channel-message alert histories."""
        try:
            with open(path, "r") as f:
                return set(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()
        except Exception as e:
            print(f"[{self.bot_id}] Failed to load {path}: {e}")
            return set()

    def _save_id_set(self, path, id_set):
        """Persist a set of IDs to disk as a JSON list."""
        try:
            with open(path, "w") as f:
                json.dump(list(id_set), f)
        except Exception as e:
            print(f"[{self.bot_id}] Failed to save {path}: {e}")

    def _member_has_exempt_role(self, member):
        return any(role.id in self.auto_kick_exempt_role_ids for role in member.roles)

    def _member_has_flagged_role(self, member):
        return self.auto_kick_role_id and any(role.id == self.auto_kick_role_id for role in member.roles)

    def _member_is_flagged(self, member):
        if not self._member_has_flagged_role(member):
            return False
        if self._member_has_exempt_role(member):
            return False
        if member.id in self.alerted_member_ids:
            return False
        return True

    async def _alert_staff_of_flagged_member(self, member):
        """Notify staff (pinging the configured staff role) that a single
        member has been found holding the flagged role. Used for individual
        events (member join, role update).

        Each member is only ever alerted on once. The alert is recorded to
        disk immediately so the member is never pinged about again, even
        across restarts.

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

        if member.id in self.alerted_member_ids:
            return

        self.alerted_member_ids.add(member.id)
        self._save_id_set(self.alerted_members_file, self.alerted_member_ids)

        await self._report_to_staff(
            f"{member.mention} has just been given the "
            f"<@&{self.auto_kick_role_id}> role. Please review.",
            ping_role=True
        )

    async def _scan_guilds_for_forbidden_role(self):
        """Bulk scan run on startup. Alerts staff once per member currently
        holding the flagged role who hasn't already been alerted on.

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

            for member in flagged:
                await self._alert_staff_of_flagged_member(member)
                await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # Watched-channel message alert (alerts staff instead of kicking)
    # ------------------------------------------------------------------

    async def _get_watched_channel_first_message_id(self, channel):
        """Return (and cache) the ID of the watched channel's first-ever
        message, so it can be excluded from triggering alerts -- it's
        assumed to be a pinned info/rules message, not a real post.

        Looked up lazily on the first message seen in the channel rather
        than at startup, so it still works correctly even if the channel
        wasn't cached yet at on_ready."""
        if self._watched_channel_first_message_id is not None:
            return self._watched_channel_first_message_id

        try:
            async for first_message in channel.history(limit=1, oldest_first=True):
                self._watched_channel_first_message_id = first_message.id
                return self._watched_channel_first_message_id
        except Exception as e:
            print(f"[{self.bot_id}] Could not fetch first message of watched channel: {e}")

        return None

    async def _alert_staff_of_watched_channel_message(self, message):
        """Notify staff that a member has posted in the watched channel.

        This mirrors the flagged-role alert in every other respect: each
        member is only ever alerted on once, permanently (persisted to
        disk, so it holds across restarts), and only the bot instance with
        enable_lfg=True sends these alerts, to avoid duplicate staff pings
        if multiple instances share the same staff channel.

        The one deliberate difference: exempt roles are NOT checked here.
        Posting in this channel is itself the signal being watched for, so
        it applies regardless of any roles the member holds.
        """
        if not self.enable_lfg:
            return

        if not self.watched_message_channel_id:
            return

        if message.channel.id != self.watched_message_channel_id:
            return

        if message.author.bot:
            return

        first_message_id = await self._get_watched_channel_first_message_id(message.channel)
        if first_message_id is not None and message.id == first_message_id:
            return

        if message.author.id in self.alerted_message_author_ids:
            return

        self.alerted_message_author_ids.add(message.author.id)
        self._save_id_set(self.alerted_message_authors_file, self.alerted_message_author_ids)

        await self._report_to_staff(
            f"{message.author.mention} has posted in "
            f"{message.jump_url}. Please review.",
            ping_role=True
        )

    def _setup_events(self):
        """Setup bot events."""
        @self.client.event
        async def on_ready():
            print(f"[{self.bot_id}] {self.client.user} online")
            if self.auto_kick_role_id:
                await self._scan_guilds_for_forbidden_role()
            asyncio.create_task(self.update_server_status())

            if self.enable_lfg:
                # Re-attach working buttons to every still-pending QOTD
                # suggestion so they survive a bot restart.
                for entry in self.qotd_entries:
                    if entry["status"] == "pending" and entry.get("message_id"):
                        view = self._build_qotd_view(entry)
                        if view:
                            try:
                                self.client.add_view(view, message_id=entry["message_id"])
                            except Exception as e:
                                print(f"[{self.bot_id}] Could not re-register QOTD view: {e}")

                if self.qotd_daily_task is not None and not self.qotd_daily_task.is_running():
                    self.qotd_daily_task.start()
                    next_run = self.qotd_daily_task.next_iteration
                    print(f"[{self.bot_id}] QOTD daily task started -- next run at "
                          f"{next_run.isoformat() if next_run else 'unknown'} "
                          f"(target: {self.qotd_post_hour_utc:02d}:{self.qotd_post_minute_utc:02d} UTC)")

            # All commands are registered as guild commands (not global),
            # so they show up instantly instead of waiting up to an hour
            # for a global sync to propagate.
            try:
                for guild in self.client.guilds:
                    try:
                        self.client.tree.copy_global_to(guild=guild)
                        synced_guild = await self.client.tree.sync(guild=guild)
                        print(f"[{self.bot_id}] Synced {len(synced_guild)} command(s) to guild "
                              f"'{guild.name}' ({guild.id}): {[c.name for c in synced_guild]}")
                    except discord.Forbidden:
                        print(f"[{self.bot_id}] Missing 'applications.commands' scope in guild "
                              f"'{guild.name}' ({guild.id}) -- re-invite the bot with that scope enabled.")
                    except Exception as e:
                        print(f"[{self.bot_id}] Error syncing commands to guild {guild.id}: {e}")

                # One-time cleanup: wipe any commands left over from a
                # previous global sync so they don't show up as duplicates
                # alongside the guild commands above. Safe to run every
                # startup -- it's a no-op once there's nothing global left.
                self.client.tree.clear_commands(guild=None)
                await self.client.tree.sync()
            except Exception as e:
                print(f"[{self.bot_id}] Error syncing commands: {e}")

        @self.client.event
        async def on_guild_join(guild):
            # Make sure commands are available immediately in any guild
            # the bot is added to after startup.
            try:
                self.client.tree.copy_global_to(guild=guild)
                synced_guild = await self.client.tree.sync(guild=guild)
                print(f"[{self.bot_id}] Synced {len(synced_guild)} command(s) to newly-joined guild "
                      f"'{guild.name}' ({guild.id}): {[c.name for c in synced_guild]}")
            except discord.Forbidden:
                print(f"[{self.bot_id}] Missing 'applications.commands' scope in guild "
                      f"'{guild.name}' ({guild.id}) -- re-invite the bot with that scope enabled.")
            except Exception as e:
                print(f"[{self.bot_id}] Error syncing commands to newly-joined guild {guild.id}: {e}")

        @self.client.event
        async def on_member_join(member):
            await self._alert_staff_of_flagged_member(member)

        @self.client.event
        async def on_message(message):
            await self._alert_staff_of_watched_channel_message(message)
            # No prefix commands are defined on this bot (only slash
            # commands), but process_commands is called for forward
            # compatibility in case any get added later.
            await self.client.process_commands(message)

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
        """Update server status and manage LFG messages.

        Uses the scplist.kr v2 players endpoint, which accepts a batch of
        server IDs and returns only the ones currently online (offline
        servers are simply omitted from the response array). Polled once
        every self.refresh (60s) seconds, well under the API's rate limit
        of 20 requests/60s per client IP.
        """
        while True:
            try:
                url = 'https://api.scplist.kr/api/v2/servers/players'
                resp = requests.get(url, params={'serverIds': [self.server_id]})

                if resp.status_code == 429:
                    print(f"[{self.bot_id}] scplist.kr API rate limit hit, will retry next cycle.")
                    await asyncio.sleep(self.refresh)
                    continue

                if resp.status_code != 200:
                    print(f"[{self.bot_id}] scplist.kr API returned {resp.status_code}: {resp.text[:200]}")
                    await asyncio.sleep(self.refresh)
                    continue

                data = resp.json()
                entry = next(
                    (s for s in data if str(s.get("serverId")) == str(self.server_id)),
                    None,
                )

                if entry is None:
                    # Server wasn't in the response -- it's offline.
                    is_offline = True
                    sl_pc, max_pc = 0, 0
                    player_count = "Offline"
                else:
                    is_offline = False
                    sl_pc = int(entry["current"])
                    max_pc = int(entry["max"])
                    player_count = f"{sl_pc}/{max_pc}"

                self.current_player_count = player_count
                player_counts[self.bot_id] = player_count

                # Update presence
                if is_offline:
                    status = discord.Status.invisible
                elif sl_pc == 0:
                    status = discord.Status.idle
                elif sl_pc >= max_pc:
                    status = discord.Status.dnd
                else:
                    status = discord.Status.online

                activity_name = "Server Offline" if is_offline else f"Online: {player_count}"
                await self.client.change_presence(
                    activity=discord.CustomActivity(name=activity_name),
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