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

DICTIONARY_API_URL = "https://api.dictionaryapi.dev/api/v2/entries/en/{}"


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


def _build_dictionary_embed(word: str, data: list) -> discord.Embed:
    """Build a rich embed from an api.dictionaryapi.dev response."""
    phonetic = data[0].get("phonetic")
    if not phonetic:
        for entry in data:
            for p in entry.get("phonetics", []):
                if p.get("text"):
                    phonetic = p["text"]
                    break
            if phonetic:
                break

    embed = discord.Embed(
        title=word.capitalize(),
        description=f"*{phonetic}*" if phonetic else None,
        color=discord.Color.blurple(),
    )

    seen_pos = set()
    synonyms = []
    field_count = 0

    for entry in data:
        for meaning in entry.get("meanings", []):
            pos = meaning.get("partOfSpeech", "unknown")
            if pos in seen_pos or field_count >= 4:
                continue
            seen_pos.add(pos)
            field_count += 1

            lines = []
            for i, d in enumerate(meaning.get("definitions", [])[:2], start=1):
                line = f"{i}. {d['definition']}"
                if d.get("example"):
                    line += f"\n*e.g. {d['example']}*"
                lines.append(line)
                for syn in d.get("synonyms", [])[:3]:
                    if syn not in synonyms:
                        synonyms.append(syn)

            embed.add_field(name=pos.capitalize(), value="\n".join(lines), inline=False)

    if synonyms:
        embed.add_field(name="Synonyms", value=", ".join(synonyms[:8]), inline=False)

    embed.set_footer(text="Source: Free Dictionary API")
    return embed


@bot.tree.command(name="define", description="Define a word")
async def define(interaction: discord.Interaction, word: str):
    await interaction.response.defer()
    clean_word = word.strip()

    # Try a real dictionary first
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            url = DICTIONARY_API_URL.format(urllib.parse.quote(clean_word.lower()))
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    embed = _build_dictionary_embed(clean_word, data)
                    await interaction.followup.send(embed=embed)
                    return
    except Exception:
        pass  # fall through to Wikipedia below

    # Fallback for names, places, and concepts that aren't dictionary words
    try:
        wikipedia.set_lang("en")
        summary = wikipedia.summary(clean_word, sentences=3, auto_suggest=True)
        embed = discord.Embed(
            title=clean_word.title(),
            description=summary,
            color=discord.Color.blurple(),
        )
        try:
            page = wikipedia.page(clean_word, auto_suggest=True)
            embed.url = page.url
        except Exception:
            pass
        embed.set_footer(text="No dictionary entry found — showing a Wikipedia summary")
        await interaction.followup.send(embed=embed)
    except wikipedia.exceptions.DisambiguationError as e:
        await interaction.followup.send(
            f"**{clean_word}** could mean several things:\n" + ", ".join(e.options[:5])
        )
    except wikipedia.exceptions.PageError:
        await interaction.followup.send(f"Could not find a definition for `{clean_word}`.")
    except Exception as e:
        await interaction.followup.send(f"Error: `{e}`")


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
