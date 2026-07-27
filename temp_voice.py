import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import discord
from discord.ext import commands

logger = logging.getLogger("necro_bot")

HUB_CHANNEL_ID = 1530935069819146431
DASHBOARD_CHANNEL_ID = 1531106553627934850

# Used to find and reuse the dashboard message across bot restarts, instead
# of spamming a new one into the channel every time the bot starts up.
_DASHBOARD_MARKER = "temp-voice-dashboard"


@dataclass
class TempChannel:
    channel_id: int
    owner_id: int
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class TempVoiceService:
    """Join-to-create temporary voice channels.

    When a member joins the hub channel (HUB_CHANNEL_ID), a fresh voice
    channel named "<display name>'s VC" is created next to it and the
    member is moved into it. Once that channel is empty again (everyone
    has left, not just the creator), it's deleted automatically.

    A live embed dashboard listing all currently active temp channels is
    kept up to date in DASHBOARD_CHANNEL_ID.
    """

    def __init__(
        self,
        bot: commands.Bot,
        hub_channel_id: int = HUB_CHANNEL_ID,
        dashboard_channel_id: int = DASHBOARD_CHANNEL_ID,
    ):
        self.bot = bot
        self.hub_channel_id = hub_channel_id
        self.dashboard_channel_id = dashboard_channel_id
        self._active: dict[int, TempChannel] = {}
        self._dashboard_message: discord.Message | None = None

    async def start(self):
        """Call once during bootstrap (e.g. in on_ready)."""
        await self._ensure_dashboard_message()
        await self._refresh_dashboard()

    # ------------------------------------------------------------------
    # Event entry point — hook this up to the bot's on_voice_state_update.
    # ------------------------------------------------------------------
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if after.channel is not None and after.channel.id == self.hub_channel_id:
            await self._create_temp_channel(member)
            return

        relevant = False

        if before.channel is not None and before.channel.id in self._active:
            relevant = True
            if len(before.channel.members) == 0:
                await self._delete_temp_channel(before.channel.id)
                return

        if after.channel is not None and after.channel.id in self._active:
            relevant = True

        if relevant:
            await self._refresh_dashboard()

    # ------------------------------------------------------------------
    # Channel lookups
    # ------------------------------------------------------------------
    def _hub_channel(self) -> discord.VoiceChannel | None:
        channel = self.bot.get_channel(self.hub_channel_id)
        return channel if isinstance(channel, discord.VoiceChannel) else None

    def _dashboard_channel(self) -> discord.TextChannel | None:
        channel = self.bot.get_channel(self.dashboard_channel_id)
        return channel if isinstance(channel, discord.TextChannel) else None

    # ------------------------------------------------------------------
    # Create / delete temp channels
    # ------------------------------------------------------------------
    async def _create_temp_channel(self, member: discord.Member):
        hub = self._hub_channel()
        if hub is None:
            logger.warning("Hub voice channel %s not found.", self.hub_channel_id)
            return

        guild = hub.guild
        name = f"{member.display_name}'s VC"[:100]

        # Copy the hub's permission overwrites so the temp channel behaves
        # the same way visibility/access-wise, then layer on owner control.
        overwrites = {
            target: discord.PermissionOverwrite.from_pair(*ow.pair())
            for target, ow in hub.overwrites.items()
        }
        owner_overwrite = overwrites.get(member, discord.PermissionOverwrite())
        owner_overwrite.update(manage_channels=True, move_members=True, connect=True)
        overwrites[member] = owner_overwrite

        try:
            temp_channel = await guild.create_voice_channel(
                name=name,
                category=hub.category,
                overwrites=overwrites,
                bitrate=hub.bitrate,
                user_limit=hub.user_limit,
                reason=f"Temporary VC for {member} (joined hub).",
            )
            await member.move_to(temp_channel, reason="Moved to their new temporary VC.")
        except discord.Forbidden:
            logger.exception("Missing permissions to create/move into a temp VC.")
            return
        except discord.HTTPException:
            logger.exception("Failed to create temp VC for %s", member)
            return

        self._active[temp_channel.id] = TempChannel(
            channel_id=temp_channel.id, owner_id=member.id
        )
        logger.info("Created temp VC %s ('%s') for %s", temp_channel.id, name, member)
        await self._refresh_dashboard()

    async def _delete_temp_channel(self, channel_id: int):
        self._active.pop(channel_id, None)
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            try:
                await channel.delete(reason="Temporary VC is empty.")
                logger.info("Deleted empty temp VC %s", channel_id)
            except discord.NotFound:
                pass
            except discord.HTTPException:
                logger.exception("Failed to delete temp VC %s", channel_id)
        await self._refresh_dashboard()

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------
    async def _ensure_dashboard_message(self):
        channel = self._dashboard_channel()
        if channel is None:
            logger.warning("Dashboard channel %s not found.", self.dashboard_channel_id)
            return

        async for message in channel.history(limit=50):
            if (
                message.author.id == self.bot.user.id
                and message.embeds
                and (message.embeds[0].footer.text or "") == _DASHBOARD_MARKER
            ):
                self._dashboard_message = message
                return

        embed = self._build_dashboard_embed()
        try:
            self._dashboard_message = await channel.send(embed=embed)
        except discord.HTTPException:
            logger.exception("Failed to post temp VC dashboard message.")

    def _build_dashboard_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🔊 Active Temporary Voice Channels",
            color=discord.Color.blurple(),
        )

        lines = []
        for info in self._active.values():
            channel = self.bot.get_channel(info.channel_id)
            if channel is None:
                continue
            owner = channel.guild.get_member(info.owner_id)
            owner_text = owner.mention if owner else f"<@{info.owner_id}>"
            created = discord.utils.format_dt(info.created_at, style="R")
            lines.append(
                f"**{channel.name}** — owner {owner_text} — "
                f"{len(channel.members)} member(s) — created {created}"
            )

        embed.description = "\n".join(lines) if lines else (
            "No temporary voice channels are active right now."
        )
        embed.set_footer(text=_DASHBOARD_MARKER)
        embed.timestamp = discord.utils.utcnow()
        return embed

    async def _refresh_dashboard(self):
        if self._dashboard_message is None:
            await self._ensure_dashboard_message()
            if self._dashboard_message is None:
                return

        embed = self._build_dashboard_embed()
        try:
            await self._dashboard_message.edit(embed=embed)
        except discord.NotFound:
            self._dashboard_message = None
            await self._ensure_dashboard_message()
        except discord.HTTPException:
            logger.exception("Failed to refresh temp VC dashboard.")