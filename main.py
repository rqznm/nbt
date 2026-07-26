import os
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

# Oxford Dictionaries API is not free — you need your own credentials from
# https://developer.oxforddictionaries.com/ (sandbox tier is free but capped
# at 500 calls). Leave these unset and the bot will just skip Oxford.
OXFORD_APP_ID = os.getenv("OXFORD_APP_ID")
OXFORD_APP_KEY = os.getenv("OXFORD_APP_KEY")
OXFORD_API_BASE = os.getenv(
    "OXFORD_API_BASE", "https://od-api.oxforddictionaries.com/api/v2/entries/en-us/"
)


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


async def _fetch_oxford(session: aiohttp.ClientSession, word: str):
    """Query the Oxford Dictionaries API. Returns parsed JSON, or None
    (either because it's not configured, or the word wasn't found)."""
    if not (OXFORD_APP_ID and OXFORD_APP_KEY):
        return None
    url = OXFORD_API_BASE + urllib.parse.quote(word.lower())
    headers = {"app_id": OXFORD_APP_ID, "app_key": OXFORD_APP_KEY}
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception:
        pass
    return None


def _fetch_wikipedia(word: str):
    """Blocking Wikipedia lookup — call via asyncio.to_thread.
    Returns a dict with either {"summary", "url"} or {"disambiguation": [...]},
    or None if nothing was found."""
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


def _format_oxford(data: dict, limit: int = 3) -> str:
    """Parse Oxford Dictionaries API v2 'entries' response into a plain-text
    block, one paragraph per part of speech.
    Note: written from Oxford's documented schema, not tested against a live
    key — double check field names once you have real credentials, in case
    Oxford has tweaked the response shape since."""
    blocks = []
    for result in data.get("results", []):
        for lex in result.get("lexicalEntries", []):
            if len(blocks) >= limit:
                return "\n\n".join(blocks)
            category = lex.get("lexicalCategory", {}).get("text", "Unknown")
            lines = []
            for entry in lex.get("entries", []):
                for sense in entry.get("senses", [])[:2]:
                    for definition in sense.get("definitions", [])[:1]:
                        line = f"{len(lines) + 1}. {definition}"
                        examples = sense.get("examples", [])
                        if examples:
                            line += f' (e.g. "{examples[0].get("text", "")}")'
                        lines.append(line)
            if lines:
                blocks.append(f"**Oxford — {category}**\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def _format_wikipedia(wiki_result: dict) -> str:
    text = f"**Wikipedia**\n{wiki_result['summary']}"
    if wiki_result.get("url"):
        text += f"\n{wiki_result['url']}"
    return text


@bot.tree.command(name="define", description="Define a word using multiple dictionaries")
async def define(interaction: discord.Interaction, word: str):
    await interaction.response.defer()
    clean_word = word.strip()

    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        oxford_data = await _fetch_oxford(session, clean_word)

    wiki_result = await asyncio.to_thread(_fetch_wikipedia, clean_word)
    have_wiki_summary = bool(wiki_result and "summary" in wiki_result)

    if not oxford_data and not have_wiki_summary:
        if wiki_result and "disambiguation" in wiki_result:
            await interaction.followup.send(
                f"**{clean_word}** could mean several things:\n"
                + ", ".join(wiki_result["disambiguation"])
            )
        else:
            await interaction.followup.send(f"Could not find a definition for `{clean_word}`.")
        return

    parts = [f"**{clean_word.capitalize()}**"]

    if oxford_data:
        oxford_text = _format_oxford(oxford_data)
        if oxford_text:
            parts.append(oxford_text)

    if have_wiki_summary:
        parts.append(_format_wikipedia(wiki_result))

    sources = []
    if oxford_data:
        sources.append("Oxford Dictionaries API")
    if have_wiki_summary:
        sources.append("Wikipedia")
    parts.append(f"*Sources: {', '.join(sources)}*")

    text = "\n\n".join(parts)
    if len(text) > 1900:
        text = text[:1897] + "..."

    await interaction.followup.send(text)


def _run_web_search(query: str, max_results: int):
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

    embed = discord.Embed(
        title=f"Search results for \u201c{query}\u201d",
        color=discord.Color.blurple(),
    )
    for i, hit in enumerate(hits, start=1):
        title = hit.get("title") or "Untitled"
        if len(title) > 100:
            title = title[:97] + "..."
        href = hit.get("href") or ""
        body = (hit.get("body") or "").strip()
        if len(body) > 220:
            body = body[:217].rsplit(" ", 1)[0] + "..."
        value = f"{body}\n{href}" if body else href
        embed.add_field(name=f"{i}. {title}", value=value or "\u200b", inline=False)
    embed.set_footer(text="Results via DuckDuckGo")
    await interaction.followup.send(embed=embed)


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
