"""
ai_chat.py
==========

Routes every user message posted in one specific channel to a locally
hosted llama.cpp server running:

    DavidAU/Qwen3.6-27B-Fable-Fusion-711-Uncensored-Heretic-NM-DAU-NEO-MAX-MTP-GGUF

and turns the reply into three edits of a single Discord message:

    1. "rena ai is thinking..."                      (sent immediately)
    2. the model's live <think>...</think> reasoning  (edited in, plaintext)
    3. the final answer only, thinking text removed   (last edit)

Every request is stateless: only the triggering message's own text is
sent to the model. Nothing is stored or replayed across messages.

--------------------------------------------------------------------------
REQUIRED: a llama.cpp server must already be running and reachable at
LLAMA_SERVER_URL below. This module is only the Discord-facing half.
See setup_llama_server.sh for building llama.cpp, downloading a GGUF
quant of the model, and running it as a systemd service.

IMPORTANT — hardware note for the vps-f9eebf0d.vps.ovh.net box (4 vCores /
8 GB RAM): the smallest quant of this 27B model is ~11.7 GB on its own,
which does not fit in 8 GB of RAM. Do not expect this to run until the
VPS has at least 16 GB RAM (bare minimum, smallest quant only) — 24-32 GB
is recommended. See setup_llama_server.sh for details. Running it as-is
on 8 GB will fail to load or crash into swap.
--------------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass

import aiohttp
import discord
from discord.ext import commands

logger = logging.getLogger("necro_bot.ai_chat")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Only messages in this channel are treated as prompts.
AI_CHANNEL_ID = 1533667198575575120

# Local llama.cpp server (OpenAI-compatible /v1/chat/completions endpoint).
LLAMA_SERVER_URL = "http://127.0.0.1:8080/v1/chat/completions"
MODEL_NAME = "DavidAU/Qwen3.6-27B-Fable-Fusion-711-Uncensored-Heretic-NM-DAU-NEO-MAX-MTP-GGUF"

SYSTEM_PROMPT = (
    "You are rena ai, a helpful assistant replying in a Discord server. "
    "Respond only to the user's current message below. You have no memory "
    "of any earlier conversation, so do not refer to prior messages."
)

THINKING_LABEL = "🧠 **rena ai is thinking...**"

# CPU inference of a 27B model is heavy. Keep this at 1 — running two
# generations at once on a 4-core box just makes both of them slower,
# it doesn't get you real parallelism.
MAX_CONCURRENT_GENERATIONS = 1

# Overall queue depth before new prompts get told to back off.
MAX_QUEUE_SIZE = 20

# Minimum seconds between edits of the "thinking" message while streaming.
EDIT_INTERVAL_SECONDS = 1.5

DISCORD_MESSAGE_LIMIT = 2000

# Sampling / generation params sent to the model.
REQUEST_TEMPERATURE = 0.7
REQUEST_MAX_TOKENS = 4096


@dataclass
class Job:
    message: discord.Message
    prompt: str


class ResponseAssembler:
    """
    Buckets streamed model output into `reasoning` (thought process) and
    `answer` text.

    Works two ways:
      - If the server sends a separate `reasoning_content` delta field
        (newer llama.cpp builds with --reasoning-format do this), that is
        used directly.
      - Otherwise, raw `content` text is scanned for inline
        <think>...</think> tags (the default for Qwen3-style thinking
        models) and split accordingly, even if a tag is split across two
        streamed chunks.
    """

    def __init__(self) -> None:
        self.reasoning: str = ""
        self.answer: str = ""
        self._raw: str = ""
        self._native_reasoning: bool = False

    def feed_reasoning(self, piece: str) -> None:
        self._native_reasoning = True
        self.reasoning += piece

    def feed_content(self, piece: str) -> None:
        if self._native_reasoning:
            # Server already separated reasoning out for us.
            self.answer += piece
            return
        self._raw += piece
        self._resplit()

    def _resplit(self) -> None:
        text = self._raw
        lower = text.lower()
        think_start = lower.find("<think>")
        if think_start == -1:
            # Model never opened a think block (or isn't a thinking model) —
            # treat everything as the answer.
            self.answer = text
            return
        content_start = think_start + len("<think>")
        think_end = lower.find("</think>", content_start)
        if think_end == -1:
            self.reasoning = text[content_start:].strip()
            self.answer = ""
        else:
            self.reasoning = text[content_start:think_end].strip()
            self.answer = text[think_end + len("</think>"):].lstrip("\n")


class AIChatService:
    """Owns the request queue, the HTTP session to llama.cpp, and the
    single background worker that turns prompts into edited replies."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self._pending_users: set[int] = set()
        self._session: aiohttp.ClientSession | None = None
        self._workers: list[asyncio.Task] = []

    async def start(self) -> None:
        if self._session is not None:
            return  # already started
        self._session = aiohttp.ClientSession()
        self._workers = [
            asyncio.create_task(self._worker_loop())
            for _ in range(MAX_CONCURRENT_GENERATIONS)
        ]
        logger.info("AIChatService started (%d worker(s))", len(self._workers))

    async def close(self) -> None:
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Entry point called from on_message
    # ------------------------------------------------------------------

    async def handle_message(self, message: discord.Message) -> None:
        if message.channel.id != AI_CHANNEL_ID:
            return
        if message.author.bot:
            return

        content = message.content.strip()
        if not content:
            return  # nothing text-based to send (attachment-only message, etc.)

        prefix = self.bot.command_prefix
        if isinstance(prefix, str) and content.startswith(prefix):
            return  # let normal bot commands through untouched

        if message.author.id in self._pending_users:
            await message.reply(
                "⏳ You already have a response in progress here — please wait "
                "for it to finish before sending another prompt.",
                mention_author=True,
            )
            return

        job = Job(message=message, prompt=content)
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            await message.reply(
                "🚦 rena ai is swamped with requests right now — please try "
                "again in a bit.",
                mention_author=True,
            )
            return

        self._pending_users.add(message.author.id)

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    async def _worker_loop(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._process_job(job)
            except Exception:
                logger.exception("Unexpected error processing AI job for %s", job.message.author)
            finally:
                self._pending_users.discard(job.message.author.id)
                self._queue.task_done()

    async def _process_job(self, job: Job) -> None:
        try:
            placeholder = await job.message.reply(THINKING_LABEL, mention_author=True)
        except discord.HTTPException:
            logger.exception("Could not send placeholder reply for %s", job.message.id)
            return

        assembler = ResponseAssembler()
        last_edit = 0.0

        try:
            async for kind, piece in self._stream_completion(job.prompt):
                if kind == "reasoning":
                    assembler.feed_reasoning(piece)
                else:
                    assembler.feed_content(piece)

                if assembler.answer:
                    # Thinking phase is over — let it finish generating the
                    # answer without further "thinking" edits.
                    continue

                now = time.monotonic()
                if assembler.reasoning and (now - last_edit) >= EDIT_INTERVAL_SECONDS:
                    await self._safe_edit(placeholder, self._format_thinking(assembler.reasoning))
                    last_edit = now

            final_text = assembler.answer.strip() or assembler.reasoning.strip()
            if not final_text:
                final_text = "*(rena ai didn't return any text for that one — try rephrasing?)*"
            await self._send_final(placeholder, job.message, final_text)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("AI generation failed for message %s", job.message.id)
            await self._safe_edit(
                placeholder,
                "⚠️ rena ai couldn't generate a response — the model server may "
                "be unreachable or overloaded. Please try again shortly.",
            )

    # ------------------------------------------------------------------
    # llama.cpp streaming call
    # ------------------------------------------------------------------

    async def _stream_completion(self, prompt: str):
        assert self._session is not None, "AIChatService.start() was never called"

        payload = {
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": True,
            "temperature": REQUEST_TEMPERATURE,
            "max_tokens": REQUEST_MAX_TOKENS,
        }

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=300)

        async with self._session.post(LLAMA_SERVER_URL, json=payload, timeout=timeout) as resp:
            resp.raise_for_status()
            async for raw_line in resp.content:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                reasoning_piece = delta.get("reasoning_content")
                content_piece = delta.get("content")
                if reasoning_piece:
                    yield "reasoning", reasoning_piece
                if content_piece:
                    yield "content", content_piece

    # ------------------------------------------------------------------
    # Discord message helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_thinking(text: str) -> str:
        body = text.strip()
        prefix = f"{THINKING_LABEL}\n\n"
        budget = DISCORD_MESSAGE_LIMIT - len(prefix) - 40
        if len(body) > budget:
            body = "…(earlier thoughts trimmed)…\n" + body[-budget:]
        return prefix + body if body else prefix

    async def _send_final(
        self,
        placeholder: discord.Message,
        orig_message: discord.Message,
        text: str,
    ) -> None:
        chunks = self._chunk_text(text)
        await self._safe_edit(placeholder, chunks[0])
        for extra in chunks[1:]:
            with contextlib.suppress(discord.HTTPException):
                await orig_message.channel.send(extra)

    @staticmethod
    def _chunk_text(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n", 0, limit)
            if split_at < limit // 2:
                split_at = limit
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip("\n")
        return chunks

    @staticmethod
    async def _safe_edit(message: discord.Message, content: str) -> None:
        try:
            await message.edit(content=content[:DISCORD_MESSAGE_LIMIT])
        except discord.NotFound:
            logger.warning("Placeholder message %s was deleted before it could be edited", message.id)
        except discord.HTTPException:
            logger.exception("Failed to edit AI response message %s", message.id)