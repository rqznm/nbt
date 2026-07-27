import logging

import discord

from settings_store import SettingsStore


logger = logging.getLogger("welcome")


async def send_welcome(member: discord.Member, store: SettingsStore) -> None:
    settings = store.welcome()
    channel_id = settings.get("channel_id")
    message_template = settings.get("message") or "welcome {mention}"
    if not channel_id:
        return

    channel = member.guild.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await member.guild.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.warning("Welcome channel %s is unavailable.", channel_id)
            return

    message = message_template.replace("{mention}", member.mention)
    try:
        await channel.send(
            message,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
    except discord.Forbidden:
        logger.warning("Missing permission to send welcome message in %s.", channel_id)
    except discord.HTTPException:
        logger.exception("Failed to send welcome message in %s.", channel_id)
