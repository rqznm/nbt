import asyncio
import logging
import re
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("necro_bot")

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)

_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def parse_duration(duration: str | None) -> timedelta | None:
    """Parse a duration string like '30m', '2h', '7d' into a timedelta.

    Returns None if duration is None/empty (meaning permanent).
    Raises ValueError if the string doesn't match the expected format.
    """
    if duration is None or not duration.strip():
        return None

    match = _DURATION_RE.match(duration)
    if not match:
        raise ValueError(
            "Duration must look like '30s', '10m', '2h', '7d', or '1w'."
        )

    amount, unit = match.groups()
    seconds = int(amount) * _UNIT_SECONDS[unit.lower()]
    return timedelta(seconds=seconds)


def format_duration(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    for unit_seconds, label in (
        (604800, "week"),
        (86400, "day"),
        (3600, "hour"),
        (60, "minute"),
        (1, "second"),
    ):
        if total_seconds % unit_seconds == 0 and total_seconds >= unit_seconds:
            count = total_seconds // unit_seconds
            return f"{count} {label}{'s' if count != 1 else ''}"
    return f"{total_seconds} seconds"


class ModerationCommands(commands.Cog):
    """Slash commands for banning and kicking members."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Tracks scheduled unban tasks so temporary bans can be lifted.
        # NOTE: this is in-memory only. If the bot restarts, pending
        # temporary bans will no longer be auto-lifted; consider persisting
        # (e.g. via SettingsStore) if that matters for your use case.
        self._pending_unbans: dict[tuple[int, int], asyncio.Task] = {}

    async def cog_unload(self):
        for task in self._pending_unbans.values():
            task.cancel()

    async def _schedule_unban(
        self,
        guild: discord.Guild,
        user: discord.abc.Snowflake,
        delay_seconds: float,
    ):
        key = (guild.id, user.id)
        try:
            await asyncio.sleep(delay_seconds)
            await guild.unban(user, reason="Temporary ban duration expired.")
            logger.info("Auto-unbanned user %s in guild %s", user.id, guild.id)
        except discord.NotFound:
            # Already unbanned manually, ignore.
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Failed to auto-unban user %s in guild %s", user.id, guild.id
            )
        finally:
            self._pending_unbans.pop(key, None)

    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.describe(
        member="The member to kick.",
        reason="Why this member is being kicked.",
    )
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided.",
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        if member.id == interaction.user.id:
            await interaction.response.send_message(
                "You can't kick yourself.", ephemeral=True
            )
            return

        if member.top_role >= interaction.guild.me.top_role:
            await interaction.response.send_message(
                "I can't kick that member — their role is equal to or higher than mine.",
                ephemeral=True,
            )
            return

        try:
            try:
                await member.send(
                    f"You have been kicked from **{interaction.guild.name}**.\n"
                    f"Reason: {reason}"
                )
            except discord.Forbidden:
                pass

            await member.kick(reason=f"{reason} (by {interaction.user})")
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to kick that member.", ephemeral=True
            )
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"Failed to kick member: {exc}", ephemeral=True
            )
            return

        logger.info(
            "%s kicked %s (%s) — reason: %s",
            interaction.user,
            member,
            member.id,
            reason,
        )
        embed = discord.Embed(
            title="Member Kicked",
            description=f"{member.mention} was kicked.",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text=f"Kicked by {interaction.user}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="ban", description="Ban a member from the server.")
    @app_commands.describe(
        member="The member to ban.",
        reason="Why this member is being banned.",
        duration="Optional. e.g. '30m', '2h', '7d', '1w'. Leave blank for a permanent ban.",
        delete_message_days="How many days of their recent messages to delete (0-7). Default 0.",
    )
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided.",
        duration: str | None = None,
        delete_message_days: app_commands.Range[int, 0, 7] = 0,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        if member.id == interaction.user.id:
            await interaction.response.send_message(
                "You can't ban yourself.", ephemeral=True
            )
            return

        if member.top_role >= interaction.guild.me.top_role:
            await interaction.response.send_message(
                "I can't ban that member — their role is equal to or higher than mine.",
                ephemeral=True,
            )
            return

        try:
            delta = parse_duration(duration)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        duration_text = format_duration(delta) if delta else "permanent"

        try:
            try:
                dm_text = (
                    f"You have been banned from **{interaction.guild.name}**.\n"
                    f"Reason: {reason}\n"
                    f"Duration: {duration_text}"
                )
                await member.send(dm_text)
            except discord.Forbidden:
                pass  # Member has DMs disabled; proceed anyway.

            await member.ban(
                reason=f"{reason} (by {interaction.user})",
                delete_message_seconds=delete_message_days * 86400,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to ban that member.", ephemeral=True
            )
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"Failed to ban member: {exc}", ephemeral=True
            )
            return

        if delta is not None:
            key = (interaction.guild.id, member.id)
            existing = self._pending_unbans.get(key)
            if existing:
                existing.cancel()
            task = asyncio.create_task(
                self._schedule_unban(interaction.guild, member, delta.total_seconds())
            )
            self._pending_unbans[key] = task

        logger.info(
            "%s banned %s (%s) — reason: %s — duration: %s",
            interaction.user,
            member,
            member.id,
            reason,
            duration_text,
        )
        embed = discord.Embed(
            title="Member Banned",
            description=f"{member.mention} was banned.",
            color=discord.Color.red(),
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Duration", value=duration_text, inline=False)
        embed.set_footer(text=f"Banned by {interaction.user}")
        await interaction.response.send_message(embed=embed)

    @kick.error
    @ban.error
    async def moderation_error_handler(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        if isinstance(error, app_commands.BotMissingPermissions):
            await interaction.response.send_message(
                "I'm missing the permissions needed to do that.", ephemeral=True
            )
            return

        logger.exception("Unhandled error in moderation command.", exc_info=error)
        if interaction.response.is_done():
            await interaction.followup.send(
                "Something went wrong running that command.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Something went wrong running that command.", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCommands(bot))