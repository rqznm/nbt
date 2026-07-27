"""Behavior for a single "images only" channel:

- Any message without an image (attachment OR a link straight to an image
  file) is deleted immediately.
- Any message that does contain an image gets reacted to, in order, with a
  fixed set of emojis.
- Each user may only have one of those reactions active on a message at a
  time -- picking a different one swaps out their previous pick.
- A sticky message ("React with how you feel") is re-sent after every image
  so it always sits at the bottom of the channel, and the previous sticky
  message is deleted so there is never more than one.
"""

import asyncio
import logging
import re

import discord

logger = logging.getLogger("necro_bot")

STICKY_CHANNEL_ID = 1531394477602636087

STICKY_MESSAGE_TEXT = "React with the image to rate it"

# Applied to every qualifying image message, in this exact order. Also the
# only emojis subject to the one-reaction-per-user rule.
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

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".heic")

URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)


def _url_points_to_image(url: str) -> bool:
    # Strip query string / fragment before checking the extension, e.g.
    # ".../image.png?width=800" or ".../image.jpg#preview".
    path = url.split("?", 1)[0].split("#", 1)[0]
    return path.lower().endswith(IMAGE_EXTENSIONS)


def _has_image(message: discord.Message) -> bool:
    """True if the message has an image attachment or a link directly to one.

    Note: this only catches links that point straight at an image file
    (an extension we recognize). A link to a page that merely *unfurls*
    into an image preview (e.g. most Twitter/X or gallery links) won't be
    detected -- Discord doesn't resolve that embed until after the message
    has already been handled here.
    """
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            return True
        if attachment.filename.lower().endswith(IMAGE_EXTENSIONS):
            return True
    for url in URL_PATTERN.findall(message.content):
        if _url_points_to_image(url):
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

    async def handle_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Enforce one active reaction per user on messages in the channel.

        When a user adds one of REACTION_EMOJIS, any other REACTION_EMOJIS
        reaction that same user already has on the message gets removed --
        so picking a new emoji swaps out their previous pick.

        Requires the bot to have the Manage Messages permission in the
        channel (removing another user's reaction needs it).
        """
        if payload.channel_id != STICKY_CHANNEL_ID:
            return
        if payload.user_id == self.bot.user.id:
            return

        emoji = str(payload.emoji)
        if emoji not in REACTION_EMOJIS:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except discord.HTTPException:
                logger.exception("Could not resolve channel %s", payload.channel_id)
                return

        message = channel.get_partial_message(payload.message_id)
        member = discord.Object(id=payload.user_id)

        for other_emoji in REACTION_EMOJIS:
            if other_emoji == emoji:
                continue
            try:
                await message.remove_reaction(other_emoji, member)
            except discord.NotFound:
                pass  # user hadn't reacted with this one -- nothing to do
            except discord.HTTPException:
                logger.exception(
                    "Failed to remove reaction %s for user %s", other_emoji, payload.user_id
                )

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