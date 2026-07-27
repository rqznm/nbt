"""Behavior for a single "images only" channel:

- Any message without an image attachment is deleted immediately.
- Any message that *does* contain an image gets reacted to, in order, with a
  fixed set of emojis.
- A sticky message ("React with how you feel") is re-sent after every image
  so it always sits at the bottom of the channel, and the previous sticky
  message is deleted so there is never more than one.
"""

import asyncio
import logging

import discord

logger = logging.getLogger("necro_bot")

STICKY_CHANNEL_ID = 1531394477602636087

STICKY_MESSAGE_TEXT = "React with how you feel"

# Applied to every qualifying image message, in this exact order.
REACTION_EMOJIS = [
    "💯",  # :100:
    "🤤",  # :drooling_face:
    "🥹",  # :face_holding_back_tears:
    "😋",  # :yum:
    "✅",  # :white_check_mark:
    "👍",  # :thumbsup:
    "👎",  # :thumbsdown:
    "🙄",  # :rolling_eyes:
    "😔",  # :pensive:
    "🤮",  # :face_vomiting:
]

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".heic")


def _has_image(message: discord.Message) -> bool:
    """True if the message has at least one image attachment.

    Only attachments count (not link previews/embeds), so a caption posted
    alongside an image is fine, but a text-only or link-only message is not.
    """
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            return True
        if attachment.filename.lower().endswith(IMAGE_EXTENSIONS):
            return True
    return False


class StickyReactionChannelService:
    """Owns the image-only / auto-react / sticky-message behavior for one channel."""

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

        if not _has_image(message):
            await self._delete(message)
            return True

        await self._react_in_order(message)
        await self._resend_sticky(message.channel)
        return True

    async def ensure_initial_sticky(self) -> None:
        """Call once on startup.

        Cleans up any sticky messages left over from a previous run and posts
        a fresh one, so the channel never ends up with duplicates after a
        restart even if no new image gets posted right away.
        """
        channel = self.bot.get_channel(STICKY_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(STICKY_CHANNEL_ID)
            except discord.HTTPException:
                logger.exception("Could not find sticky channel %s", STICKY_CHANNEL_ID)
                return

        async with self._lock:
            await self._purge_old_stickies(channel)
            self._sticky_message = await channel.send(STICKY_MESSAGE_TEXT)

    async def _purge_old_stickies(self, channel: discord.abc.Messageable) -> None:
        async for old_message in channel.history(limit=50):
            if (
                old_message.author.id == self.bot.user.id
                and old_message.content == STICKY_MESSAGE_TEXT
            ):
                await self._delete(old_message)

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