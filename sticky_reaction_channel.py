"""Behavior for a single channel:

- A message is deleted only if it is *just* plain text and/or emoji -- no
  attachments, no embeds, no stickers, no links. Everything else (images,
  files, voice messages, links/URLs of any kind) is left alone.
- Anything that survives gets reacted to, in order, with a fixed set of
  emojis.
- A sticky message ("React with how you feel") is re-sent after every
  surviving message so it always sits at the bottom of the channel, and the
  previous sticky message (only ever the bot's own prior sticky text) is
  deleted so there is never more than one.
"""

import asyncio
import logging
import re

import discord

logger = logging.getLogger("necro_bot")

STICKY_CHANNEL_ID = 1531394477602636087

STICKY_MESSAGE_TEXT = "React to a message to rate it!"

# Applied to every surviving message, in this exact order.
REACTION_EMOJIS = [
    "\U0001F4AF",  # :100:
    "\U0001F924",  # :drooling_face:
    "\U0001F979",  # :face_holding_back_tears:
    "\U0001F60B",  # :yum:
    "\U00002705",  # :white_check_mark:
    "\U0001F44D",  # :thumbsup:
    "\U0001F44E",  # :thumbsdown:
    "\U0001F644",  # :rolling_eyes:
    "\U0001F614",  # :pensive:
    "\U0001F92E",  # :face_vomiting:
]

URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)


def _is_plain_text_or_emoji(message: discord.Message) -> bool:
    """True only for messages with nothing but words and/or emoji in them.

    Anything with an attachment (image, file, voice message -- voice
    messages arrive as an audio attachment, so this covers them too), an
    embed, a sticker, or a link/URL of any kind returns False and is kept.
    """
    if message.attachments:
        return False
    if message.embeds:
        return False
    if message.stickers:
        return False
    if URL_PATTERN.search(message.content):
        return False
    return True


class StickyReactionChannelService:
    """Owns the plain-text-cleanup / auto-react / sticky-message behavior for one channel."""

    def __init__(self, bot: discord.Client):
        self.bot = bot
        self._sticky_message: discord.Message | None = None
        self._lock = asyncio.Lock()

    async def handle_message(self, message: discord.Message) -> bool:
        """Process a message if it belongs to the sticky channel.

        Returns True if the message was handled (and no other handler should
        touch it), False if it belongs to a different channel.
        """
        if message.channel.id != STICKY_CHANNEL_ID:
            return False

        if _is_plain_text_or_emoji(message):
            await self._delete(message)
            return True

        await self._react_in_order(message)
        await self._resend_sticky(message.channel)
        return True

    async def ensure_initial_sticky(self) -> None:
        """Call once on startup.

        Just posts a fresh sticky message. Does not read or touch anything
        already in the channel -- no history scan, no deletions of existing
        messages. If a sticky from a previous run is still sitting there,
        it's left alone; only messages sent *after* this point are managed
        by _resend_sticky below.
        """
        channel = self.bot.get_channel(STICKY_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(STICKY_CHANNEL_ID)
            except discord.HTTPException:
                logger.exception("Could not find sticky channel %s", STICKY_CHANNEL_ID)
                return

        async with self._lock:
            self._sticky_message = await channel.send(STICKY_MESSAGE_TEXT)

    async def _react_in_order(self, message: discord.Message) -> None:
        for emoji in REACTION_EMOJIS:
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                logger.exception(
                    "Failed to add reaction %s to message %s", emoji, message.id
                )

    async def _resend_sticky(self, channel: discord.abc.Messageable) -> None:
        async with self._lock:
            old = self._sticky_message
            self._sticky_message = await channel.send(STICKY_MESSAGE_TEXT)
            if old is not None:
                await self._delete(old)

    @staticmethod
    async def _delete(message: discord.Message) -> None:
        try:
            await message.delete()
        except discord.HTTPException:
            logger.exception("Failed to delete message %s", message.id)