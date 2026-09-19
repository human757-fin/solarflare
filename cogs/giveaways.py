import random
import re
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("giveaways")

GIVEAWAY_COLOR = 0xE91E63
ENDED_COLOR = 0x607D8B


def utcnow():
    """Naive UTC now (MySQL DATETIME has no tz)."""
    return datetime.utcnow()


def fmt_dt(dt):
    """Format a (possibly naive) datetime for Discord, treating it as UTC."""
    if dt is None:
        return "n/a"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return discord.utils.format_dt(dt, "R")


def parse_duration(s: str) -> Optional[int]:
    """Parse a compact duration like '2h30m' / '1d' / '45m' into seconds."""
    if not s:
        return None
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    total = 0
    for match in re.finditer(r"(\d+)\s*([dhms])", s.lower()):
        total += int(match.group(1)) * units[match.group(2)]
    return total or None


class GiveawayButton(discord.ui.Button):
    def __init__(self, giveaway_id: int, bot):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="🎉 Enter",
            custom_id=f"giveaway:{giveaway_id}",
        )
        self.giveaway_id = giveaway_id
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        pool = self.bot.db_pool
        if not pool:
            return await interaction.response.send_message(
                "Database is unavailable.", ephemeral=True
            )

        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, ended, extra_entries_role_id, extra_entries, winners, prize, ends_at "
                    "FROM giveaways WHERE id=%s",
                    (self.giveaway_id,),
                )
                row = await cur.fetchone()
                if not row or row[1]:
                    return await interaction.response.send_message(
                        "That giveaway has already ended.", ephemeral=True
                    )
                gid, ended, role_id, extra_entries, winners, prize, ends_at = row

                # Determine how many entries this user gets.
                entries = 1
                if role_id and extra_entries:
                    if isinstance(interaction.user, discord.Member):
                        role = interaction.user.get_role(role_id)
                        if role:
                            entries += extra_entries

                await cur.execute(
                    """
                    INSERT INTO giveaway_entries (giveaway_id, user_id, entries)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE entries = entries + VALUES(entries)
                    """,
                    (gid, interaction.user.id, entries),
                )
                await cur.execute(
                    "SELECT COALESCE(SUM(entries), 0) FROM giveaway_entries WHERE giveaway_id=%s",
                    (gid,),
                )
                total_entries = (await cur.fetchone())[0]

        # Update the live entry count on the giveaway message.
        try:
            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if embed is not None:
                for i, field in enumerate(embed.fields):
                    if field.name == "Entries":
                        embed.set_field_at(i, name="Entries", value=str(total_entries))
                        break
                await interaction.message.edit(embed=embed)
        except Exception:
            pass

        msg = f"You entered **{prize}** with {entries} {'entry' if entries == 1 else 'entries'}!"
        await interaction.response.send_message(msg, ephemeral=True)


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id: int, bot, ended: bool = False):
        super().__init__(timeout=None)
        button = GiveawayButton(giveaway_id, bot)
        if ended:
            button.disabled = True
            button.label = "Ended"
        self.add_item(button)


class Giveaways(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._views_registered = False

    async def cog_load(self):
        # Table creation must never prevent the cog from registering its commands.
        if self.bot.db_pool:
            try:
                await self._ensure_tables()
                await self._create_tables()
            except Exception:
                log.exception("Failed to create giveaway tables")

    async def _ensure_tables(self):
        # Legacy: the first release created FK-constrained tables that some
        # MySQL/MariaDB collations reject (errno 150). Drop them so the plain
        # schema below can be created cleanly on the next boot.
        async with self.bot.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS "
                    "WHERE CONSTRAINT_SCHEMA = DATABASE() "
                    "AND CONSTRAINT_NAME IN ('fk_giveaway_entries', 'fk_giveaway_winners')"
                )
                row = await cur.fetchone()
                if row and row[0]:
                    await cur.execute("SET FOREIGN_KEY_CHECKS = 0")
                    await cur.execute("DROP TABLE IF EXISTS giveaway_winners")
                    await cur.execute("DROP TABLE IF EXISTS giveaway_entries")
                    await cur.execute("DROP TABLE IF EXISTS giveaways")
                    await cur.execute("SET FOREIGN_KEY_CHECKS = 1")

    async def _create_tables(self):
        async with self.bot.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS giveaways (
                        id INT UNSIGNED NOT NULL AUTO_INCREMENT,
                        guild_id BIGINT UNSIGNED NOT NULL,
                        channel_id BIGINT UNSIGNED NOT NULL,
                        message_id BIGINT UNSIGNED NOT NULL,
                        prize VARCHAR(255) NOT NULL,
                        winners INT UNSIGNED NOT NULL DEFAULT 1,
                        ends_at DATETIME NULL,
                        hosted_by BIGINT UNSIGNED NOT NULL,
                        extra_entries_role_id BIGINT UNSIGNED NULL,
                        extra_entries INT UNSIGNED NOT NULL DEFAULT 0,
                        ended TINYINT(1) NOT NULL DEFAULT 0,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (id),
                        KEY idx_active (ended, ends_at),
                        KEY idx_message (message_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS giveaway_entries (
                        giveaway_id INT UNSIGNED NOT NULL,
                        user_id BIGINT UNSIGNED NOT NULL,
                        entries INT UNSIGNED NOT NULL DEFAULT 1,
                        PRIMARY KEY (giveaway_id, user_id),
                        KEY idx_entries_gw (giveaway_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS giveaway_winners (
                        giveaway_id INT UNSIGNED NOT NULL,
                        user_id BIGINT UNSIGNED NOT NULL,
                        PRIMARY KEY (giveaway_id, user_id),
                        KEY idx_winners_gw (giveaway_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)

    @commands.Cog.listener()
    async def on_ready(self):
        await self._register_views()
        if not self.check_ended_loop.is_running():
            self.check_ended_loop.start()

    async def _register_views(self):
        """Re-register persistent entry buttons for active giveaways (restart-proof)."""
        if self._views_registered or not self.bot.db_pool:
            return
        self._views_registered = True
        try:
            async with self.bot.db_pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id FROM giveaways WHERE ended=0"
                    )
                    rows = await cur.fetchall()
            for (gid,) in rows:
                self.bot.add_view(GiveawayView(gid, self.bot))
            log.info("Registered %s persistent giveaway views", len(rows))
        except Exception:
            log.exception("Failed to register giveaway views")

    # ------------------------------------------------------------------
    # Winner picking
    # ------------------------------------------------------------------

    async def _pick_winners(self, db_conn, giveaway_id: int, want: int, exclude: set):
        async with db_conn.cursor() as cur:
            await cur.execute(
                "SELECT user_id, entries FROM giveaway_entries WHERE giveaway_id=%s",
                (giveaway_id,),
            )
            rows = await cur.fetchall()
        pool = []
        for user_id, entries in rows:
            if user_id in exclude:
                continue
            pool.extend([user_id] * entries)
        if not pool:
            return []
        return random.sample(pool, min(want, len(pool)))

    async def _build_embed(self, giveaway: dict, entries_count: int, winners: list = None):
        ended = bool(giveaway.get("ended"))
        gid = giveaway["id"]
        ends_at = giveaway.get("ends_at")
        if ends_at and not ended:
            ends_str = fmt_dt(ends_at)
        else:
            ends_str = "Ended"
        embed = discord.Embed(
            title="🎉 Giveaway",
            description=f"**{giveaway['prize']}**",
            color=ENDED_COLOR if ended else GIVEAWAY_COLOR,
        )
        host = self.bot.get_user(giveaway["hosted_by"])
        embed.add_field(
            name="Hosted by",
            value=host.mention if host else f"<@{giveaway['hosted_by']}>",
            inline=True,
        )
        embed.add_field(
            name="Winners",
            value=str(giveaway["winners"]),
            inline=True,
        )
        embed.add_field(name="Entries", value=str(entries_count), inline=True)
        if giveaway.get("extra_entries_role_id"):
            embed.add_field(
                name="🔥 Extra entries",
                value=f"<@&{giveaway['extra_entries_role_id']}> gets **{giveaway['extra_entries']} extra** entries",
                inline=False,
            )
        embed.add_field(name="Ends", value=ends_str, inline=True)
        if winners:
            mentions = " ".join(f"<@{uid}>" for uid in winners)
            embed.add_field(name="Winner(s)", value=mentions, inline=False)
        return embed

    # ------------------------------------------------------------------
    # Giveaway lifecycle
    # ------------------------------------------------------------------

    async def _conclude(self, gid: int, force_ends_at=None):
        pool = self.bot.db_pool
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT * FROM giveaways WHERE id=%s", (gid,))
                row = await cur.fetchone()
                if not row:
                    return
                # dict via column names
                cols = [d[0] for d in cur.description]
                giveaway = dict(zip(cols, row))
                channel = self.bot.get_channel(giveaway["channel_id"])

                await cur.execute(
                    "SELECT user_id FROM giveaway_winners WHERE giveaway_id=%s",
                    (gid,),
                )
                previous_winners = {r[0] for r in await cur.fetchall()}

                winners = await self._pick_winners(
                    conn, gid, giveaway["winners"], previous_winners
                )
                for uid in winners:
                    await cur.execute(
                        "INSERT IGNORE INTO giveaway_winners (giveaway_id, user_id) VALUES (%s, %s)",
                        (gid, uid),
                    )
                await cur.execute(
                    "UPDATE giveaways SET ended=1 WHERE id=%s",
                    (gid,),
                )
                await cur.execute(
                    "SELECT COALESCE(SUM(entries), 0) FROM giveaway_entries WHERE giveaway_id=%s",
                    (gid,),
                )
                total_entries = (await cur.fetchone())[0]
                giveaway["ended"] = True

        embed = await self._build_embed(giveaway, total_entries, winners)

        try:
            msg = await channel.fetch_message(giveaway["message_id"])
            await msg.edit(embed=embed, view=GiveawayView(gid, self.bot, ended=True))
        except Exception:
            log.warning("Could not update giveaway message %s", gid)

        if winners:
            mentions = " ".join(f"<@{uid}>" for uid in winners)
            desc = f"**{giveaway['prize']}**"
            win_embed = discord.Embed(
                title="🎉 Giveaway Ended",
                description=f"Congratulations {mentions}!\nYou won **{giveaway['prize']}**!",
                color=ENDED_COLOR,
            )
            try:
                await channel.send(embed=win_embed)
            except Exception:
                log.warning("Could not announce winners for giveaway %s", gid)
        else:
            try:
                await channel.send("No entries — nobody won. 😕")
            except Exception:
                pass

    @tasks.loop(seconds=15)
    async def check_ended_loop(self):
        if not self.bot.db_pool:
            return
        now = utcnow()
        async with self.bot.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM giveaways WHERE ended=0 AND ends_at IS NOT NULL AND ends_at <= %s",
                    (now,),
                )
                rows = await cur.fetchall()
        for (gid,) in rows:
            try:
                await self._conclude(gid)
            except Exception:
                log.exception("Failed to conclude giveaway %s", gid)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    giveaway = app_commands.Group(name="giveaway", description="Giveaway commands")

    @giveaway.command(name="start", description="Start a giveaway")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        prize="What is being given away",
        channel="Channel to post the giveaway in",
        winners="Number of winners (default 1)",
        duration="Duration like 2h30m / 1d (default 30m)",
        extra_role="Role that gets extra entries",
        extra_entries="How many extra entries that role gets (default 1)",
    )
    async def give_start(
        self,
        interaction: discord.Interaction,
        prize: str,
        channel: Optional[discord.TextChannel] = None,
        winners: int = 1,
        duration: str = "30m",
        extra_role: Optional[discord.Role] = None,
        extra_entries: int = 1,
    ):
        if not self.bot.db_pool:
            return await interaction.response.send_message("Database is unavailable.", ephemeral=True)
        if winners < 1:
            winners = 1
        secs = parse_duration(duration)
        if not secs:
            return await interaction.response.send_message(
                "Invalid duration. Use something like `2h30m`, `1d`, `45m`.", ephemeral=True
            )
        channel = channel or interaction.channel
        ends_at = utcnow() + timedelta(seconds=secs)

        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(
            title="🎉 Giveaway",
            description=f"**{prize}**",
            color=GIVEAWAY_COLOR,
        )
        embed.add_field(
            name="Hosted by", value=interaction.user.mention, inline=True
        )
        embed.add_field(name="Winners", value=str(winners), inline=True)
        embed.add_field(name="Entries", value="0", inline=True)
        if extra_role:
            embed.add_field(
                name="🔥 Extra entries",
                value=f"{extra_role.mention} gets **{extra_entries} extra** entries",
                inline=False,
            )
        embed.add_field(name="Ends", value=fmt_dt(ends_at), inline=True)

        async with self.bot.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO giveaways
                        (guild_id, channel_id, message_id, prize, winners, ends_at,
                         hosted_by, extra_entries_role_id, extra_entries, ended)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0)
                    """,
                    (
                        interaction.guild.id,
                        channel.id,
                        0,
                        prize,
                        winners,
                        ends_at,
                        interaction.user.id,
                        extra_role.id if extra_role else None,
                        extra_entries if extra_role else 0,
                    ),
                )
                gid = cur.lastrowid

        msg = await channel.send(embed=embed, view=GiveawayView(gid, self.bot))
        async with self.bot.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE giveaways SET message_id=%s WHERE id=%s", (msg.id, gid)
                )

        await interaction.followup.send(
            f"Giveaway started in {channel.mention}! Ends {fmt_dt(ends_at)}",
            ephemeral=True,
        )

    @giveaway.command(name="end", description="End a giveaway early and pick winners")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(message_id="Giveaway message ID or link")
    async def give_end(self, interaction: discord.Interaction, message_id: str):
        gid = await self._find_giveaway(message_id)
        if not gid:
            return await interaction.response.send_message("Giveaway not found.", ephemeral=True)
        async with self.bot.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE giveaways SET ends_at=%s WHERE id=%s",
                    (utcnow(), gid),
                )
        await self._conclude(gid)
        await interaction.response.send_message("Giveaway ended and winners announced!", ephemeral=True)

    @giveaway.command(name="reroll", description="Reroll the winners of an ended giveaway")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(message_id="Giveaway message ID or link")
    async def give_reroll(self, interaction: discord.Interaction, message_id: str):
        gid = await self._find_giveaway(message_id)
        if not gid:
            return await interaction.response.send_message("Giveaway not found.", ephemeral=True)
        pool = self.bot.db_pool
        if not pool:
            return await interaction.response.send_message("Database is unavailable.", ephemeral=True)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT * FROM giveaways WHERE id=%s", (gid,))
                row = await cur.fetchone()
                cols = [d[0] for d in cur.description]
                giveaway = dict(zip(cols, row))
                if not giveaway["ended"]:
                    return await interaction.response.send_message(
                        "Giveaway is still running — end it first.", ephemeral=True
                    )
                await cur.execute(
                    "SELECT user_id FROM giveaway_winners WHERE giveaway_id=%s", (gid,)
                )
                previous = {r[0] for r in await cur.fetchall()}
                new_winners = await self._pick_winners(
                    conn, gid, giveaway["winners"], previous
                )
                for uid in new_winners:
                    await cur.execute(
                        "INSERT IGNORE INTO giveaway_winners (giveaway_id, user_id) VALUES (%s, %s)",
                        (gid, uid),
                    )
                await cur.execute(
                    "SELECT COALESCE(SUM(entries), 0) FROM giveaway_entries WHERE giveaway_id=%s",
                    (gid,),
                )
                total_entries = (await cur.fetchone())[0]

        channel = self.bot.get_channel(giveaway["channel_id"])
        embed = await self._build_embed(giveaway, total_entries, new_winners)
        if channel:
            try:
                msg = await channel.fetch_message(giveaway["message_id"])
                await msg.edit(embed=embed, view=GiveawayView(gid, self.bot, ended=True))
            except Exception:
                pass
            if new_winners:
                mentions = " ".join(f"<@{uid}>" for uid in new_winners)
                await channel.send(
                    f"🎉 **New winner(s)**: {mentions} for **{giveaway['prize']}**!"
                )
            else:
                await channel.send("No entries available to reroll. 😕")
        await interaction.response.send_message("Reroll complete.", ephemeral=True)

    @giveaway.command(name="edit", description="Edit an active giveaway")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        message_id="Giveaway message ID or link",
        prize="New prize",
        winners="New winner count",
        duration="Extend duration like 1h",
    )
    async def give_edit(
        self,
        interaction: discord.Interaction,
        message_id: str,
        prize: Optional[str] = None,
        winners: Optional[int] = None,
        duration: Optional[str] = None,
    ):
        gid = await self._find_giveaway(message_id)
        if not gid:
            return await interaction.response.send_message("Giveaway not found.", ephemeral=True)
        pool = self.bot.db_pool
        if not pool:
            return await interaction.response.send_message("Database is unavailable.", ephemeral=True)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT * FROM giveaways WHERE id=%s", (gid,))
                row = await cur.fetchone()
                cols = [d[0] for d in cur.description]
                giveaway = dict(zip(cols, row))
                if giveaway["ended"]:
                    return await interaction.response.send_message(
                        "Cannot edit an ended giveaway.", ephemeral=True
                    )
                updates = []
                values = []
                if prize:
                    updates.append("prize=%s")
                    values.append(prize)
                    giveaway["prize"] = prize
                if winners and winners >= 1:
                    updates.append("winners=%s")
                    values.append(winners)
                    giveaway["winners"] = winners
                if duration:
                    secs = parse_duration(duration)
                    if not secs:
                        return await interaction.response.send_message(
                            "Invalid duration.", ephemeral=True
                        )
                    new_end = utcnow() + timedelta(seconds=secs)
                    updates.append("ends_at=%s")
                    values.append(new_end)
                    giveaway["ends_at"] = new_end
                if updates:
                    await cur.execute(
                        f"UPDATE giveaways SET {', '.join(updates)} WHERE id=%s",
                        (*values, gid),
                    )
                await cur.execute(
                    "SELECT COALESCE(SUM(entries), 0) FROM giveaway_entries WHERE giveaway_id=%s",
                    (gid,),
                )
                total_entries = (await cur.fetchone())[0]

        channel = self.bot.get_channel(giveaway["channel_id"])
        if channel:
            try:
                msg = await channel.fetch_message(giveaway["message_id"])
                embed = await self._build_embed(giveaway, total_entries)
                await msg.edit(embed=embed)
            except Exception:
                pass
        await interaction.response.send_message("Giveaway updated!", ephemeral=True)

    @giveaway.command(name="list", description="List active giveaways in this server")
    async def give_list(self, interaction: discord.Interaction):
        if not self.bot.db_pool:
            return await interaction.response.send_message("Database is unavailable.", ephemeral=True)
        async with self.bot.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, prize, winners, ends_at, message_id, channel_id "
                    "FROM giveaways WHERE guild_id=%s AND ended=0 ORDER BY ends_at ASC",
                    (interaction.guild.id,),
                )
                rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("No active giveaways.", ephemeral=True)
        lines = []
        for gid, prize, winners, ends_at, message_id, channel_id in rows:
            link = f"https://discord.com/channels/{interaction.guild.id}/{channel_id}/{message_id}"
            lines.append(f"• **{prize}** — {winners} winner(s) — ends {fmt_dt(ends_at)} — [jump]({link})")
        embed = discord.Embed(
            title="Active Giveaways",
            description="\n".join(lines),
            color=GIVEAWAY_COLOR,
        )
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------

    async def _find_giveaway(self, message_id: str) -> Optional[int]:
        """Resolve a message ID or discord.com link to a giveaway ID."""
        digits = re.sub(r"\D", "", message_id)
        if not digits or not self.bot.db_pool:
            return None
        async with self.bot.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM giveaways WHERE message_id=%s", (int(digits),)
                )
                row = await cur.fetchone()
        return row[0] if row else None


async def setup(bot):
    await bot.add_cog(Giveaways(bot))