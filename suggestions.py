import discord

from settings_store import SettingsStore


MAX_SUGGESTION_LENGTH = 1800


def clean_suggestion(value: str) -> str:
    suggestion = value.strip()
    if not suggestion:
        raise ValueError("Suggestion cannot be empty.")
    if len(suggestion) > MAX_SUGGESTION_LENGTH:
        raise ValueError(f"Suggestion must be {MAX_SUGGESTION_LENGTH} characters or less.")
    return suggestion


def save_suggestion(interaction: discord.Interaction, store: SettingsStore, suggestion: str) -> None:
    user = interaction.user
    guild_id = interaction.guild.id if interaction.guild else None
    channel_id = interaction.channel.id if interaction.channel else None
    store.add_suggestion(
        author_id=user.id,
        author_name=str(user),
        suggestion=suggestion,
        guild_id=guild_id,
        channel_id=channel_id,
    )