import os
import re
import html
import asyncio
import datetime
import subprocess
import urllib.parse

import aiohttp
import discord
import wikipedia
from ddgs import DDGS
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

WIKTIONARY_API_URL = "https://en.wiktionary.org/api/rest_v1/page/definition/{}"
GROKIPEDIA_URL = "https://grokipedia.com/page/{}"


@bot.event
async def on_ready():
    try:
        commits = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"],
            text=True
        ).strip()
    except Exception:
        commits = "?"
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game(f"version {commits}")
    )
    await bot.tree.sync()
    print(f"Logged in as {bot.user} ({bot.user.id})")


def _strip_html(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


async def _fetch_wiktionary(session, word):
    url = WIKTIONARY_API_URL.format(urllib.parse.quote(word.lower()))
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("en")
    except Exception:
        pass
    return None


async def _fetch_grokipedia(session, word):
    slug = urllib.parse.quote(word.strip().title().replace(" ", "_"))
    url = GROKIPEDIA_URL.format(slug)
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                return None
            raw = await resp.text()
    except Exception:
        return None

    raw = re.sub(r"<script.*?</script>", "", raw, flags=re.S)
    raw = re.sub(r"<style.*?</style>", "", raw, flags=re.S)
    match = re.search(r"<h1[^>]*>.*?</h1>(.*?)(?:<h2|<table)", raw, re.S)
    if not match:
        return None
    snippet = re.sub(r"\s+", " ", _strip_html(match.group(1))).strip()
    if not snippet:
        return None
    if len(snippet) > 500:
        snippet = snippet[:497] + "..."
    return {"summary": snippet, "url": url}


def _fetch_wikipedia(word):
    wikipedia.set_lang("en")
    try:
        summary = wikipedia.summary(word, sentences=2, auto_suggest=True)
        url = None
        try:
            url = wikipedia.page(word, auto_suggest=True).url
        except Exception:
            pass
        return {"summary": summary, "url": url}
    except wikipedia.exceptions.DisambiguationError as e:
        return {"disambiguation": e.options[:5]}
    except Exception:
        return None


def _format_wiktionary(entries, limit=3):
    blocks = []
    for entry in entries[:limit]:
        pos = entry.get("partOfSpeech", "Unknown")
        lines = []
        for i, d in enumerate(entry.get("definitions", [])[:2], start=1):
            definition = _strip_html(d.get("definition", ""))
            if not definition:
                continue
            line = f"{i}. {definition}"
            examples = d.get("parsedExamples") or []
            if examples:
                example_text = _strip_html(examples[0].get("example", ""))
                if example_text:
                    line += f' (e.g. "{example_text}")'
            lines.append(line)
        if lines:
            blocks.append(f"**Wiktionary — {pos}**\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def _format_source_summary(label, result):
    text = f"**{label}**\n{result['summary']}"
    if result.get("url"):
        text += f"\n{result['url']}"
    return text


@bot.tree.command(name="define", description="Define a word using multiple dictionaries")
async def define(interaction: discord.Interaction, word: str):
    await interaction.response.defer()
    clean_word = word.strip()

    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        wiktionary_entries, grokipedia_result = await asyncio.gather(
            _fetch_wiktionary(session, clean_word),
            _fetch_grokipedia(session, clean_word),
        )

    wiki_result = await asyncio.to_thread(_fetch_wikipedia, clean_word)
    have_wiki_summary = bool(wiki_result and "summary" in wiki_result)

    if not wiktionary_entries and not grokipedia_result and not have_wiki_summary:
        if wiki_result and "disambiguation" in wiki_result:
            await interaction.followup.send(
                f"**{clean_word}** could mean several things:\n"
                + ", ".join(wiki_result["disambiguation"])
            )
        else:
            await interaction.followup.send(f"Could not find a definition for `{clean_word}`.")
        return

    parts = [f"**{clean_word.capitalize()}**"]

    if wiktionary_entries:
        wikt_text = _format_wiktionary(wiktionary_entries)
        if wikt_text:
            parts.append(wikt_text)

    if have_wiki_summary:
        parts.append(_format_source_summary("Wikipedia", wiki_result))

    if grokipedia_result:
        parts.append(_format_source_summary("Grokipedia", grokipedia_result))

    sources = []
    if wiktionary_entries:
        sources.append("Wiktionary")
    if have_wiki_summary:
        sources.append("Wikipedia")
    if grokipedia_result:
        sources.append("Grokipedia")
    parts.append(f"*Sources: {', '.join(sources)}*")

    text = "\n\n".join(parts)
    if len(text) > 1900:
        text = text[:1897] + "..."

    await interaction.followup.send(text)


def _run_web_search(query, max_results):
    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results))


@bot.tree.command(name="search", description="Search the web")
@app_commands.describe(query="What to search for", results="How many results to show (1-10)")
async def search(
    interaction: discord.Interaction,
    query: str,
    results: app_commands.Range[int, 1, 10] = 5,
):
    await interaction.response.defer()
    try:
        hits = await asyncio.to_thread(_run_web_search, query, results)
    except Exception as e:
        await interaction.followup.send(f"Search failed: `{e}`")
        return

    if not hits:
        await interaction.followup.send(f"No results found for **{query}**.")
        return

    lines = [f"**Search results for \u201c{query}\u201d**"]
    for i, hit in enumerate(hits, start=1):
        title = hit.get("title") or "Untitled"
        href = hit.get("href") or ""
        body = (hit.get("body") or "").strip()
        if len(body) > 220:
            body = body[:217].rsplit(" ", 1)[0] + "..."
        entry = f"{i}. **{title}**"
        if body:
            entry += f"\n{body}"
        if href:
            entry += f"\n{href}"
        lines.append(entry)

    text = "\n\n".join(lines)
    if len(text) > 1900:
        text = text[:1897] + "..."

    await interaction.followup.send(text)


@bot.event
async def on_member_join(member):
    channel = bot.get_channel(1530727587989426336)
    if channel:
        await channel.send(
            f"welcome to necro's server {member.mention}"
        )


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    content = message.content.lower()
    slur_list = [
        "nigger",
        "faggot",
        "nigga",
        "fag"
    ]
    for slur in slur_list:
        if slur in content:
            await message.delete()
            if message.author.guild_permissions.moderate_members:
                return
            await message.author.timeout(
                datetime.timedelta(minutes=1),
                reason="Using prohibited language"
            )
            return
    if message.content.startswith("!"):
        text = message.content[1:]
        if text:
            await message.channel.send(text)
        await message.delete()
    await bot.process_commands(message)


bot.run(os.getenv("TOKEN"))
