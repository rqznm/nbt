"""
ai_chat.py
==========

Runs
    DavidAU/Qwen3.6-27B-Fable-Fusion-711-Uncensored-Heretic-NM-DAU-NEO-MAX-MTP-GGUF
directly inside this process via llama-cpp-python. No separate server —
just this file plus the pip package.

Routes every user message posted in one specific channel to the model and
turns the reply into three edits of a single Discord message:

    1. "rena ai is thinking..."                      (sent immediately)
    2. the model's live <think>...</think> reasoning  (edited in, plaintext)
    3. the final answer only, thinking text removed   (last edit)

Every request is stateless: only the triggering message's own text is sent
to the model. Nothing is stored or replayed across messages.

--------------------------------------------------------------------------
ONE-TIME SETUP on the VPS, inside your venv:

    sudo apt install -y cmake build-essential
    pip install llama-cpp-python huggingface_hub

(llama-cpp-python has no prebuilt CPU wheel on PyPI, so this compiles the
C++ core locally — takes a few minutes, only needed once.)

The FIRST time the bot starts, AIChatService.start() will download the
GGUF file (~11.7 GB for the smallest quant) from Hugging Face into
~/.cache/huggingface and load it into RAM before the bot finishes
connecting. That download is cached on disk afterwards, but the RAM load
itself happens again on every restart, since the model now lives inside
this same process instead of a separate long-running server.

HARDWARE NOTE (vps-f9eebf0d.vps.ovh.net — 4 vCores / 8 GB RAM): the
smallest available quant of this model is 11.7 GB on its own — more than
this box's total RAM. Expect the load to fail outright or get OOM-killed
on 8 GB. Because the model now runs inside the bot's own process, if
that happens it takes the whole bot down with it, not just this one
feature. This file logs a warning at startup if it detects <16 GB RAM,
but it does not stop you from trying anyway. Realistic fix: resize the
VPS to at least 16 GB (24-32 GB is comfortable), or point MODEL_REPO /
MODEL_FILE below at a smaller model that actually fits.
--------------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import discord
from discord.ext import commands
from llama_cpp import Llama

logger = logging.getLogger("necro_bot.ai_chat")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Only messages in this channel are treated as prompts.
AI_CHANNEL_ID = 1533667198575575120

MODEL_REPO = "DavidAU/Qwen3.6-27B-Fable-Fusion-711-Uncensored-Heretic-NM-DAU-NEO-MAX-MTP-GGUF"
# Smallest quant on the repo (still 11.7 GB) — see module docstring above.
MODEL_FILE = "Qwen3.6-27B-Fable-Fus-711-UnHeretic-NM-DAU-NEO-MAX-NEO-IQ2_M.gguf"

N_CTX = 4096          # context window; kept modest to control RAM use
N_THREADS = 4         # matches the VPS's 4 vCores
N_GPU_LAYERS = 0       # no GPU on this box — pure CPU inference

_MIN_RECOMMENDED_RAM_GB = 16

SYSTEM_PROMPT = (
    "You are rena ai, a helpful assistant replying in a Discord server. "
    "Respond only to the user's current message below. You have no memory "
    "of any earlier conversation, so do not refer to prior messages."
)

THINKING_LABEL = "🧠 **rena ai is thinking...**"

# Overall queue depth before new prompts get told to back off.
MAX_QUEUE_SIZE = 20

# Minimum seconds between edits of the "thinking" message while streaming.
EDIT_INTERVAL_SECONDS = 1.5

DISCORD_MESSAGE_LIMIT = 2000

# Sampling / generation params.
REQUEST_TEMPERATURE = 0.7
REQUEST_MAX_TOKENS = 4096


def _total_ram_gb() -> float | None:
    """Best-effort total system RAM in GB (Linux only, stdlib only)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except OSError:
        pass
    return None


@dataclass
class Job:
    message: discord.Message
    prompt: str


class ResponseAssembler:
    """
    Buckets streamed model output into `reasoning` (thought process) and
    `answer` text.

    Works two ways:
      - If a `reasoning_content` delta field is present (some setups emit
        this natively), it's used directly.
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
            self.answer += piece
            return
        self._raw += piece
        self._resplit()

    def _resplit(self) -> None:
        text = self._raw
        lower = text.lower()
        think_start = lower.find("<think>")
        if think_start == -1:
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
    """Owns the loaded model, the request queue, and the single background
    worker that turns prompts into edited replies."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self._pending_users: set[int] = set()
        self._llm: Llama | None = None
        self._worker_task: asyncio.Task | None = None
        # One worker thread total: this guarantees the model load and every
        # single generation run strictly one at a time. CPU inference of a
        # 27B model doesn't get faster by overlapping calls on a 4-core
        # box — it just makes every request slower.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rena-llm")

    async def start(self) -> None:
        if self._llm is not None or self._worker_task is not None:
            return  # already started

        ram = _total_ram_gb()
        if ram is not None and ram < _MIN_RECOMMENDED_RAM_GB:
            logger.warning(
                "Only ~%.1fGB RAM detected. %s alone is ~11.7GB — the model "
                "load may fail or the whole bot process may get OOM-killed. "
                "See the top of ai_chat.py for options.",
                ram, MODEL_FILE,
            )

        logger.info("Loading %s — this can take a while and a lot of RAM...", MODEL_FILE)
        loop = asyncio.get_running_loop()
        self._llm = await loop.run_in_executor(self._executor, self._load_model)
        logger.info("Model loaded, rena ai is ready.")

        self._worker_task = asyncio.create_task(self._worker_loop())

    def _load_model(self) -> Llama:
        return Llama.from_pretrained(
            repo_id=MODEL_REPO,
            filename=MODEL_FILE,
            n_ctx=N_CTX,
            n_threads=N_THREADS,
            n_gpu_layers=N_GPU_LAYERS,
            verbose=False,
        )

    async def close(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
        self._executor.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Entry point called from on_message
    # ------------------------------------------------------------------

    async def handle_message(self, message: discord.Message) -> None:
        if message.channel.id != AI_CHANNEL_ID:
            return
        if message.author.bot:
            return

        if self._llm is None:
            await message.reply(
                "⏳ rena ai is still loading the model — try again in a bit.",
                mention_author=True,
            )
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
                "⚠️ rena ai hit an error generating a response. Please try again.",
            )

    # ------------------------------------------------------------------
    # In-process model call, bridged onto the asyncio event loop
    # ------------------------------------------------------------------

    async def _stream_completion(self, prompt: str):
        """
        llama-cpp-python's create_chat_completion(stream=True) is a
        blocking, synchronous generator (each `next()` call blocks the
        calling thread while the model computes the next token). To avoid
        freezing the bot's event loop (and Discord's heartbeat) while that
        runs, the generator is driven from a background thread, and each
        chunk is handed back to the event loop via a thread-safe queue.
        """
        assert self._llm is not None, "AIChatService.start() was never called"
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        DONE = object()

        def producer() -> None:
            try:
                stream = self._llm.create_chat_completion(
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    stream=True,
                    temperature=REQUEST_TEMPERATURE,
                    max_tokens=REQUEST_MAX_TOKENS,
                )
                for chunk in stream:
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    reasoning_piece = delta.get("reasoning_content")
                    content_piece = delta.get("content")
                    if reasoning_piece:
                        loop.call_soon_threadsafe(q.put_nowait, ("reasoning", reasoning_piece))
                    if content_piece:
                        loop.call_soon_threadsafe(q.put_nowait, ("content", content_piece))
            except Exception as exc:  # surfaced to the consumer below
                loop.call_soon_threadsafe(q.put_nowait, ("error", exc))
            finally:
                loop.call_soon_threadsafe(q.put_nowait, (DONE, None))

        future = loop.run_in_executor(self._executor, producer)

        try:
            while True:
                kind, payload = await q.get()
                if kind is DONE:
                    break
                if kind == "error":
                    raise payload
                yield kind, payload
        finally:
            with contextlib.suppress(Exception):
                await future

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