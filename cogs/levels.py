import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

XP_MIN = 15
XP_MAX = 25
XP_COOLDOWN_SECONDS = 60


def xp_for_level(level: int) -> int:
    return 5 * (level ** 2) + 50 * level + 100


def progress_bar(current: int, required: int, size: int = 10) -> str:
    if required <= 0:
        return "▓" * size
    filled = round((current / required) * size)
    filled = max(0, min(size, filled))
    return "▓" * filled + "░" * (size - filled)


class Levels(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._cooldowns = {}

    async def cog_load(self):
        if not self.bot.db_pool:
            return
        async with self.bot.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS levels (
                        user_id BIGINT UNSIGNED NOT NULL,
                        guild_id BIGINT UNSIGNED NOT NULL,
                        xp BIGINT UNSIGNED NOT NULL DEFAULT 0,
                        level INT UNSIGNED NOT NULL DEFAULT 1,
                        PRIMARY KEY (user_id, guild_id)
                    )
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS level_rewards (
                        guild_id BIGINT UNSIGNED NOT NULL,
                        level INT UNSIGNED NOT NULL,
                        role_id BIGINT UNSIGNED NOT NULL,
                        PRIMARY KEY (guild_id, level)
                    )
                """)

    async def _get_progress(self, guild_id: int, user_id: int):
        async with self.bot.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT xp, level FROM levels WHERE guild_id=%s AND user_id=%s",
                    (guild_id, user_id),
                )
                return await cur.fetchone()

    async def _set_progress(self, guild_id: int, user_id: int, xp: int, level: int):
        async with self.bot.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO levels (guild_id, user_id, xp, level)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE xp=VALUES(xp), level=VALUES(level)
                    """,
                    (guild_id, user_id, xp, level),
                )

    async def _grant_level_rewards(self, member: discord.Member, level: int):
        async with self.bot.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT role_id FROM level_rewards WHERE guild_id=%s AND level<=%s",
                    (member.guild.id, level),
                )
                rows = await cur.fetchall()
        for role_id in rows:
            role = member.guild.get_role(role_id[0])
            if role and role not in member.roles:
                try:
                    await member.add_roles(role, reason=f"Level reward (level {level})")
                except discord.HTTPException:
                    pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        pool = self.bot.db_pool
        if not pool:
            return
        key = (message.guild.id, message.author.id)
        now = time.time()
        if now - self._cooldowns.get(key, 0) < XP_COOLDOWN_SECONDS:
            return
        self._cooldowns[key] = now

        gained = random.randint(XP_MIN, XP_MAX)
        row = await self._get_progress(message.guild.id, message.author.id)
        if row:
            xp, level = int(row[0]), int(row[1])
        else:
            xp, level = 0, 1

        xp += gained
        old_level = level
        required = xp_for_level(level)
        while xp >= required:
            xp -= required
            level += 1
            required = xp_for_level(level)

        await self._set_progress(message.guild.id, message.author.id, xp, level)

        if level > old_level:
            embed = discord.Embed(
                title=f"Level up! 🎉",
                description=f"{message.author.mention} reached **level {level}**!",
                color=0x43B581,
            )
            await message.channel.send(embed=embed)
            await self._grant_level_rewards(message.author, level)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    rank = app_commands.Group(name="rank", description="Level ranks")
    level = app_commands.Group(name="level", description="Level system admin")

    @rank.command(name="view", description="Show a user's rank")
    @app_commands.describe(user="User to look up (defaults to you)")
    async def rank_view(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        user = user or interaction.user
        if not self.bot.db_pool:
            return await interaction.response.send_message("Database is unavailable.", ephemeral=True)
        row = await self._get_progress(interaction.guild.id, user.id)
        if not row:
            embed = discord.Embed(
                title=user.display_name,
                description="No XP yet — send a few messages!",
                color=0x5865F2,
            )
            embed.set_thumbnail(url=user.display_avatar.url)
            return await interaction.response.send_message(embed=embed)
        xp, level = int(row[0]), int(row[1])
        required = xp_for_level(level)
        pct = round((xp / required) * 100, 1) if required else 0
        embed = discord.Embed(
            title=user.display_name,
            description=f"**Level {level}** · {xp:,}/{required:,} XP",
            color=0x5865F2,
        )
        embed.add_field(name="Progress", value=f"{progress_bar(xp, required)}  {pct}%")
        embed.set_thumbnail(url=user.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @rank.command(name="leaderboard", description="Show the top 10")
    async def rank_leaderboard(self, interaction: discord.Interaction):
        if not self.bot.db_pool:
            return await interaction.response.send_message("Database is unavailable.", ephemeral=True)
        async with self.bot.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT user_id, level, xp FROM levels WHERE guild_id=%s "
                    "ORDER BY level DESC, xp DESC LIMIT 10",
                    (interaction.guild.id,),
                )
                rows = await cur.fetchall()
        if not rows:
            return await interaction.response.send_message("No ranked users yet!", ephemeral=True)
        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, lvl, xp) in enumerate(rows, start=1):
            medal = medals[i - 1] if i <= 3 else f"`{i}.`"
            user = interaction.guild.get_member(uid)
            name = user.display_name if user else f"<@{uid}>"
            lines.append(f"{medal} **{name}** — level {lvl} ({xp:,} XP)")
        embed = discord.Embed(
            title=f"Leaderboard · {interaction.guild.name}",
            description="\n".join(lines),
            color=0xF1C40F,
        )
        await interaction.response.send_message(embed=embed)

    @level.command(name="add", description="Add XP to a user (admin)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(user="User", amount="XP amount")
    async def level_add(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        if amount < 0:
            return await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        if not self.bot.db_pool:
            return await interaction.response.send_message("Database is unavailable.", ephemeral=True)
        row = await self._get_progress(interaction.guild.id, user.id)
        if row:
            xp, level = int(row[0]), int(row[1])
        else:
            xp, level = 0, 1
        xp += amount
        old_level = level
        required = xp_for_level(level)
        while xp >= required:
            xp -= required
            level += 1
            required = xp_for_level(level)
        await self._set_progress(interaction.guild.id, user.id, xp, level)
        if level > old_level:
            await self._grant_level_rewards(user, level)
        await interaction.response.send_message(
            f"Added **{amount} XP** to {user.mention} — now level **{level}** ({xp:,}/{required:,} XP).",
            ephemeral=True,
        )

    @level.command(name="set", description="Set a user's level (admin)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(user="User", level="Target level")
    async def level_set(self, interaction: discord.Interaction, user: discord.Member, level: int):
        if level < 1:
            return await interaction.response.send_message("Level must be at least 1.", ephemeral=True)
        if not self.bot.db_pool:
            return await interaction.response.send_message("Database is unavailable.", ephemeral=True)
        await self._set_progress(interaction.guild.id, user.id, 0, level)
        await self._grant_level_rewards(user, level)
        await interaction.response.send_message(
            f"{user.mention} is now **level {level}**.", ephemeral=True
        )

    @level.command(name="rewards", description="Add a role for reaching a level (admin)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(level="Level reached", role="Role granted")
    async def level_rewards(self, interaction: discord.Interaction, level: int, role: discord.Role):
        if level < 1:
            return await interaction.response.send_message("Level must be at least 1.", ephemeral=True)
        if not self.bot.db_pool:
            return await interaction.response.send_message("Database is unavailable.", ephemeral=True)
        async with self.bot.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO level_rewards (guild_id, level, role_id)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE role_id=VALUES(role_id)
                    """,
                    (interaction.guild.id, level, role.id),
                )
        await interaction.response.send_message(
            f"Role {role.mention} will be granted at **level {level}**.", ephemeral=True
        )

    @level.command(name="clearrewards", description="Remove a level role reward (admin)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(level="Level to remove")
    async def level_clearrewards(self, interaction: discord.Interaction, level: int):
        if not self.bot.db_pool:
            return await interaction.response.send_message("Database is unavailable.", ephemeral=True)
        async with self.bot.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM level_rewards WHERE guild_id=%s AND level=%s",
                    (interaction.guild.id, level),
                )
        await interaction.response.send_message(f"Removed reward for level **{level}**.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Levels(bot))