import datetime
import logging
import re

import discord

from settings_store import SettingsStore


logger = logging.getLogger("blacklisted_words")
_pattern_cache_key: tuple[str, ...] = ()
_pattern_cache: re.Pattern[str] | None = None


def _word_pattern(word: str) -> str:
    escaped = re.escape(word)
    if re.fullmatch(r"[\w ]+", word):
        return rf"(?<!\w){escaped}s?(?!\w)"
    return escaped


def _compile_pattern(words: list[str]) -> re.Pattern[str] | None:
    global _pattern_cache_key, _pattern_cache

    clean_words = [word.strip().lower() for word in words if word.strip()]
    cache_key = tuple(clean_words)
    if cache_key == _pattern_cache_key:
        return _pattern_cache

    _pattern_cache_key = cache_key
    if not clean_words:
        _pattern_cache = None
        return None

    _pattern_cache = re.compile("|".join(_word_pattern(word) for word in clean_words), re.IGNORECASE)
    return _pattern_cache


async def delete_if_blacklisted(message: discord.Message, store: SettingsStore) -> bool:
    if message.author.bot or not message.guild or not message.content:
        return False

    pattern = _compile_pattern(store.blacklisted_words())
    if pattern is None or not pattern.search(message.content):
        return False

    try:
        await message.delete()
    except discord.NotFound:
        pass
    except discord.Forbidden:
        logger.warning("Missing permission to delete blacklisted message in %s.", message.channel.id)
    except discord.HTTPException:
        logger.exception("Failed to delete blacklisted message in %s.", message.channel.id)

    permissions = getattr(message.author, "guild_permissions", None)
    if permissions and permissions.moderate_members:
        return True

    try:
        await message.author.timeout(
            datetime.timedelta(minutes=1),
            reason="Using prohibited language",
        )
    except discord.Forbidden:
        logger.warning("Missing permission to timeout %s.", message.author.id)
    except discord.HTTPException:
        logger.exception("Failed to timeout %s.", message.author.id)
    return True