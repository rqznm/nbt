import logging

import discord

from settings_store import SETTINGS_CHANNEL_ID


logger = logging.getLogger("settings_channel_guard")


async def delete_non_bot_settings_message(message: discord.Message) -> bool:
    if message.author.bot or message.channel.id != SETTINGS_CHANNEL_ID:
        return False

    try:
        await message.delete()
    except discord.NotFound:
        pass
    except discord.Forbidden:
        logger.warning("Missing permission to delete messages in settings channel.")
    except discord.HTTPException:
        logger.exception("Failed to delete a message in the settings channel.")
    return True
