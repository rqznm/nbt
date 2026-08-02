import logging

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("necro_bot")

# (permission attribute, display label) — a curated subset of the perms
# that matter most for a quick moderation-relevant glance at a member.
_NOTABLE_PERMS = (
    ("administrator", "Administrator"),
    ("manage_guild", "Manage Server"),
    ("manage_roles", "Manage Roles"),
    ("manage_channels", "Manage Channels"),
    ("manage_messages", "Manage Messages"),
    ("manage_webhooks", "Manage Webhooks"),
    ("manage_nicknames", "Manage Nicknames"),
    ("kick_members", "Kick Members"),
    ("ban_members", "Ban Members"),
    ("moderate_members", "Timeout Members"),
    ("mention_everyone", "Mention Everyone"),
)


def _dt_to_timestamp(dt) -> str:
    """Render a datetime as a Discord timestamp: absolute + relative."""
    unix = int(dt.timestamp())
    return f"<t:{unix}:F> (<t:{unix}:R>)"


def _ordinal(n: int) -> str:
    """Render 1 -> '1st', 2 -> '2nd', 3 -> '3rd', 11 -> '11th', etc."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _notable_permissions(perms: discord.Permissions) -> list[str]:
    return [label for attr, label in _NOTABLE_PERMS if getattr(perms, attr, False)]


def _join_position(member: discord.Member) -> int | None:
    """Return this member's join-order rank in the guild, if the member
    list is fully cached (requires the members intent / a small guild)."""
    guild = member.guild
    if not guild.chunked:
        return None
    ordered = sorted(
        (m for m in guild.members if m.joined_at is not None),
        key=lambda m: m.joined_at,
    )
    try:
        return ordered.index(member) + 1
    except ValueError:
        return None


def _count_member_types(guild: discord.Guild) -> tuple[int, int]:
    """Return (humans, bots) from the cached member list, if available."""
    if not guild.chunked:
        return (0, 0)
    bots = sum(1 for m in guild.members if m.bot)
    return len(guild.members) - bots, bots


def _channel_counts(guild: discord.Guild) -> dict[str, int]:
    counts = {
        "text": 0,
        "announcement": 0,
        "voice": 0,
        "stage": 0,
        "category": 0,
        "forum": 0,
    }
    for channel in guild.channels:
        if isinstance(channel, discord.CategoryChannel):
            counts["category"] += 1
        elif isinstance(channel, discord.StageChannel):
            counts["stage"] += 1
        elif isinstance(channel, discord.ForumChannel):
            counts["forum"] += 1
        elif isinstance(channel, discord.VoiceChannel):
            counts["voice"] += 1
        elif isinstance(channel, discord.TextChannel):
            if channel.is_news():
                counts["announcement"] += 1
            else:
                counts["text"] += 1
    return counts


def _format_features(features: list[str]) -> str:
    if not features:
        return "None"
    return ", ".join(sorted(f.replace("_", " ").title() for f in features))


class InfoCommands(commands.Cog):
    """Slash commands for viewing diagnostic info about members and the server."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="user", description="Show diagnostic info about a member.")
    @app_commands.describe(member="The member to inspect. Defaults to yourself.")
    async def user(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        member = member or interaction.user
        await interaction.response.defer()

        # Member/User objects from cache/interactions don't carry banner data;
        # fetching gives us that extra bit of flair if the user has one set.
        banner_url = None
        try:
            full_user = await self.bot.fetch_user(member.id)
            if full_user.banner:
                banner_url = full_user.banner.url
        except discord.HTTPException:
            pass

        color = member.color if member.color != discord.Color.default() else discord.Color.blurple()
        embed = discord.Embed(title=f"User Info — {member}", color=color)
        embed.set_thumbnail(url=member.display_avatar.url)
        if banner_url:
            embed.set_image(url=banner_url)

        embed.add_field(name="ID", value=str(member.id), inline=True)
        embed.add_field(name="Bot Account", value="Yes" if member.bot else "No", inline=True)

        embed.add_field(name="Account Created", value=_dt_to_timestamp(member.created_at), inline=False)

        if member.joined_at:
            joined_value = _dt_to_timestamp(member.joined_at)
            position = _join_position(member)
            if position:
                joined_value += f"\n({_ordinal(position)} to join this server)"
            embed.add_field(name="Joined Server", value=joined_value, inline=False)

        if member.premium_since:
            embed.add_field(
                name="Boosting Since", value=_dt_to_timestamp(member.premium_since), inline=False
            )

        if member.is_timed_out():
            embed.add_field(
                name="Timed Out Until",
                value=_dt_to_timestamp(member.timed_out_until),
                inline=False,
            )

        embed.add_field(name="Top Role", value=member.top_role.mention, inline=True)
        embed.add_field(
            name="Role Count", value=str(max(len(member.roles) - 1, 0)), inline=True
        )

        roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
        if roles:
            roles_text = ", ".join(roles)
            if len(roles_text) > 1024:
                roles_text = roles_text[:1000] + "… (truncated)"
            embed.add_field(name="Roles", value=roles_text, inline=False)

        notable_perms = _notable_permissions(member.guild_permissions)
        embed.add_field(
            name="Permissions",
            value=", ".join(notable_perms) if notable_perms else "None",
            inline=False,
        )

        if member.voice and member.voice.channel:
            embed.add_field(name="In Voice Channel", value=member.voice.channel.mention, inline=True)

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="server", description="Show diagnostic info about this server.")
    @app_commands.checks.cooldown(1, 15.0)
    async def server(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        guild = interaction.guild
        await interaction.response.defer()

        if not guild.chunked:
            try:
                await guild.chunk()
            except (discord.ClientException, discord.HTTPException):
                # Missing the members intent, or Discord refused the request.
                # We just fall back to the member_count total below.
                pass

        embed = discord.Embed(
            title=f"Server Info — {guild.name}",
            description=guild.description,
            color=discord.Color.blurple(),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        if guild.banner:
            embed.set_image(url=guild.banner.url)

        embed.add_field(name="ID", value=str(guild.id), inline=True)
        owner_mention = guild.owner.mention if guild.owner else f"<@{guild.owner_id}>"
        embed.add_field(name="Owner", value=owner_mention, inline=True)
        embed.add_field(name="Created", value=_dt_to_timestamp(guild.created_at), inline=False)

        humans, bots = _count_member_types(guild)
        member_value = f"Total: {guild.member_count:,}"
        if guild.chunked:
            member_value += f"\nHumans: {humans:,} | Bots: {bots:,}"
        embed.add_field(name="Members", value=member_value, inline=True)

        counts = _channel_counts(guild)
        channel_value = (
            f"Text: {counts['text']} | Announcement: {counts['announcement']}\n"
            f"Voice: {counts['voice']} | Stage: {counts['stage']}\n"
            f"Category: {counts['category']} | Forum: {counts['forum']}"
        )
        embed.add_field(name="Channels", value=channel_value, inline=True)

        embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
        embed.add_field(
            name="Emojis", value=f"{len(guild.emojis)} / {guild.emoji_limit}", inline=True
        )
        embed.add_field(
            name="Stickers", value=f"{len(guild.stickers)} / {guild.sticker_limit}", inline=True
        )

        embed.add_field(
            name="Boost Status",
            value=f"Level {guild.premium_tier} — {guild.premium_subscription_count:,} boosts",
            inline=True,
        )
        embed.add_field(
            name="Verification Level", value=guild.verification_level.name.title(), inline=True
        )
        embed.add_field(
            name="Content Filter",
            value=guild.explicit_content_filter.name.replace("_", " ").title(),
            inline=True,
        )
        embed.add_field(name="NSFW Level", value=guild.nsfw_level.name.title(), inline=True)

        embed.add_field(name="Features", value=_format_features(guild.features), inline=False)

        await interaction.followup.send(embed=embed)

    @user.error
    @server.error
    async def info_error_handler(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"That command is on cooldown. Try again in {error.retry_after:.1f}s.",
                ephemeral=True,
            )
            return

        logger.exception("Unhandled error in info command.", exc_info=error)
        if interaction.response.is_done():
            await interaction.followup.send(
                "Something went wrong running that command.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Something went wrong running that command.", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(InfoCommands(bot))