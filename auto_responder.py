import logging

import discord

from settings_store import SettingsStore, normalize_keyword


logger = logging.getLogger("auto_responder")


async def send_auto_response(message: discord.Message, store: SettingsStore) -> bool:
    if message.author.bot or not message.guild or not message.content:
        return False

    content = message.content.lower()
    for rule in store.auto_responses():
        try:
            keyword = normalize_keyword(str(rule.get("keyword", "")))
        except ValueError:
            continue

        if keyword not in content:
            continue

        response = str(rule.get("response", "")).strip()
        if not response:
            continue

        response = response.replace("{mention}", message.author.mention)
        try:
            await message.channel.send(
                response,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except discord.Forbidden:
            logger.warning("Missing permission to auto-respond in %s.", message.channel.id)
        except discord.HTTPException:
            logger.exception("Failed to auto-respond in %s.", message.channel.id)
        return True

    return False
