import logging

import discord

from suggestions import clean_suggestion, save_suggestion
from settings_store import (
    MANAGER_ROLE_ID,
    SETTINGS_CHANNEL_ID,
    SettingsStore,
    normalize_keyword,
    normalize_time_value,
    parse_amount_value,
    parse_blacklisted_words,
    parse_int_id,
)


logger = logging.getLogger("settings_panel")


def _truncate(value: str, limit: int = 1024) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _code(value: object) -> str:
    cleaned = str(value).replace("`", "'")
    return f"`{cleaned}`"


async def _send_error_response(interaction: discord.Interaction) -> None:
    message = "Something went wrong while handling that setting. Check the bot logs for details."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        logger.exception("Failed to send settings error response.")


class SettingsPanel:
    def __init__(
        self,
        bot: discord.Client,
        store: SettingsStore,
        auto_delete_service,
        member_counter_service,
    ):
        self.bot = bot
        self.store = store
        self.auto_delete_service = auto_delete_service
        self.member_counter_service = member_counter_service
        self.view = SettingsView(self)

    def can_manage_settings(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        return any(role.id == MANAGER_ROLE_ID for role in member.roles)

    async def require_manager(self, interaction: discord.Interaction) -> bool:
        if self.can_manage_settings(interaction):
            return True
        await interaction.response.send_message(
            f"You need <@&{MANAGER_ROLE_ID}> to change these settings.",
            ephemeral=True,
        )
        return False

    async def ensure_panel(self) -> None:
        channel = self.bot.get_channel(SETTINGS_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(SETTINGS_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning("Settings channel %s is unavailable.", SETTINGS_CHANNEL_ID)
                return

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            logger.warning("Settings channel %s cannot receive messages.", SETTINGS_CHANNEL_ID)
            return

        embed = self.build_embed()
        panel_message_id = self.store.settings_panel_message_id()
        if panel_message_id:
            try:
                message = await channel.fetch_message(panel_message_id)
            except discord.NotFound:
                message = None
            except discord.Forbidden:
                logger.warning("Missing permission to fetch settings panel message.")
                return
            except discord.HTTPException:
                logger.exception("Failed to fetch settings panel message.")
                return

            if message is not None:
                try:
                    await message.edit(embed=embed, view=self.view)
                    return
                except discord.Forbidden:
                    logger.warning("Missing permission to edit settings panel message.")
                    return
                except discord.HTTPException:
                    logger.exception("Failed to edit settings panel message.")
                    return

        try:
            message = await channel.send(embed=embed, view=self.view)
        except discord.Forbidden:
            logger.warning("Missing permission to send settings panel.")
            return
        except discord.HTTPException:
            logger.exception("Failed to send settings panel.")
            return

        self.store.set_settings_panel_message_id(message.id)

    async def refresh_after_update(self, guild: discord.Guild | None = None) -> None:
        if guild is not None:
            await self.member_counter_service.update_guild(guild)
        await self.ensure_panel()

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Bot Settings",
            description=f"Only <@&{MANAGER_ROLE_ID}> can change settings. Suggestions are stored privately.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Auto-delete", value=self._format_auto_delete_rules(), inline=False)
        embed.add_field(name="Blacklisted Words", value=self._format_blacklisted_words(), inline=False)
        embed.add_field(name="Auto-respond", value=self._format_auto_responses(), inline=False)
        embed.add_field(name="Member Counter", value=self._format_member_counter(), inline=False)
        embed.add_field(name="Welcome", value=self._format_welcome(), inline=False)
        return embed

    def _format_auto_delete_rules(self) -> str:
        rules = self.store.auto_delete_rules()
        if not rules:
            return "No auto-delete rules set."

        lines = []
        for index, rule in enumerate(rules, start=1):
            channel_id = int(rule.get("channel_id", 0))
            time_value = rule.get("time") or "off"
            amount = rule.get("amount") or "off"
            lines.append(
                f"{index}. <#{channel_id}> ({_code(channel_id)}) - time: {_code(time_value)}, amount: {_code(amount)}"
            )
        return _truncate("\n".join(lines))

    def _format_blacklisted_words(self) -> str:
        words = self.store.blacklisted_words()
        if not words:
            return "No blacklisted words set."
        return _truncate(", ".join(_code(discord.utils.escape_mentions(word)) for word in words))

    def _format_auto_responses(self) -> str:
        rules = self.store.auto_responses()
        if not rules:
            return "No auto-responses set."

        lines = []
        for index, rule in enumerate(rules, start=1):
            keyword = discord.utils.escape_mentions(str(rule.get("keyword", "")))
            response = discord.utils.escape_mentions(str(rule.get("response", "")))
            lines.append(f"{index}. {_code(keyword)} -> {_code(response)}")
        return _truncate("\n".join(lines))

    def _format_member_counter(self) -> str:
        settings = self.store.member_counter()
        category_id = settings.get("category_id")
        template = settings.get("name_template") or "members: {counter}!"
        if not category_id:
            return f"Disabled\nFormat: {_code(template)}"
        return f"Category: <#{int(category_id)}> ({_code(category_id)})\nFormat: {_code(template)}"

    def _format_welcome(self) -> str:
        settings = self.store.welcome()
        channel_id = settings.get("channel_id")
        message = discord.utils.escape_mentions(settings.get("message") or "")
        if not channel_id:
            return f"Disabled\nMessage: {_code(message)}"
        return f"Channel: <#{int(channel_id)}> ({_code(channel_id)})\nMessage: {_code(message)}"


class SettingsView(discord.ui.View):
    def __init__(self, panel: SettingsPanel):
        super().__init__(timeout=None)
        self.panel = panel

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.error(
            "Settings button %s failed.",
            getattr(item, "custom_id", "unknown"),
            exc_info=(type(error), error, error.__traceback__),
        )
        await _send_error_response(interaction)

    @discord.ui.button(
        label="Auto-delete",
        style=discord.ButtonStyle.primary,
        custom_id="necro_settings:auto_delete",
    )
    async def auto_delete_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self.panel.require_manager(interaction):
            return
        await interaction.response.send_modal(AutoDeleteModal(self.panel))

    @discord.ui.button(
        label="Blacklist",
        style=discord.ButtonStyle.primary,
        custom_id="necro_settings:blacklist",
    )
    async def blacklist_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self.panel.require_manager(interaction):
            return
        await interaction.response.send_modal(BlacklistModal(self.panel))

    @discord.ui.button(
        label="Auto-respond",
        style=discord.ButtonStyle.primary,
        custom_id="necro_settings:auto_respond",
    )
    async def auto_respond_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self.panel.require_manager(interaction):
            return
        await interaction.response.send_modal(AutoResponseModal(self.panel))

    @discord.ui.button(
        label="Member Counter",
        style=discord.ButtonStyle.primary,
        custom_id="necro_settings:member_counter",
    )
    async def member_counter_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self.panel.require_manager(interaction):
            return
        await interaction.response.send_modal(MemberCounterModal(self.panel))

    @discord.ui.button(
        label="Welcome",
        style=discord.ButtonStyle.primary,
        custom_id="necro_settings:welcome",
    )
    async def welcome_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self.panel.require_manager(interaction):
            return
        await interaction.response.send_modal(WelcomeModal(self.panel))

    @discord.ui.button(
        label="Suggestions",
        style=discord.ButtonStyle.primary,
        custom_id="necro_settings:suggestion",
    )
    async def suggestion_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(SuggestionModal(self.panel))


class SettingsModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error(
            "Settings modal %s failed.",
            self.__class__.__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
        await _send_error_response(interaction)


class AutoDeleteModal(SettingsModal, title="Auto-delete Settings"):
    channel_id = discord.ui.TextInput(
        label="Channel ID",
        placeholder="1531073727112941688",
        required=True,
        max_length=25,
    )
    time_value = discord.ui.TextInput(
        label="Time",
        placeholder="Blank for none. Example: 5d",
        required=False,
        max_length=4,
    )
    amount = discord.ui.TextInput(
        label="Amount",
        placeholder="Blank for none. Blank time + amount removes.",
        required=False,
        max_length=12,
    )

    def __init__(self, panel: SettingsPanel):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            channel_id = parse_int_id(str(self.channel_id.value), "Channel ID")
            time_value = normalize_time_value(str(self.time_value.value))
            amount = parse_amount_value(str(self.amount.value))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        result = self.panel.store.upsert_auto_delete_rule(channel_id, time_value, amount)
        await self.panel.refresh_after_update(interaction.guild)

        if result == "saved":
            self.panel.auto_delete_service.schedule_cleanup_rule(
                {"channel_id": channel_id, "time": time_value, "amount": amount}
            )
            action = "saved"
        else:
            action = "removed"
        await interaction.followup.send(f"Auto-delete rule {action} for <#{channel_id}>.", ephemeral=True)


class BlacklistModal(SettingsModal, title="Blacklisted Words"):
    words = discord.ui.TextInput(
        label="Words",
        placeholder="Separate words with commas or new lines.",
        style=discord.TextStyle.long,
        required=False,
        max_length=4000,
    )

    def __init__(self, panel: SettingsPanel):
        super().__init__()
        self.panel = panel
        self.words.default = "\n".join(self.panel.store.blacklisted_words())[:4000]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        words = parse_blacklisted_words(str(self.words.value))
        await interaction.response.defer(ephemeral=True)
        self.panel.store.set_blacklisted_words(words)
        await self.panel.refresh_after_update(interaction.guild)
        await interaction.followup.send("Blacklisted words updated.", ephemeral=True)


class AutoResponseModal(SettingsModal, title="Keyword Auto-response"):
    keyword = discord.ui.TextInput(
        label="Keyword",
        placeholder="Example: rules",
        required=True,
        max_length=100,
    )
    response = discord.ui.TextInput(
        label="Response",
        placeholder="Blank response removes this keyword.",
        style=discord.TextStyle.long,
        required=False,
        max_length=1800,
    )

    def __init__(self, panel: SettingsPanel):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            keyword = normalize_keyword(str(self.keyword.value))
            response = str(self.response.value)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        result = self.panel.store.upsert_auto_response(keyword, response)
        await self.panel.refresh_after_update(interaction.guild)
        await interaction.followup.send(f"Auto-response {result} for `{keyword}`.", ephemeral=True)


class MemberCounterModal(SettingsModal, title="Member Counter"):
    category_id = discord.ui.TextInput(
        label="Category ID",
        placeholder="Blank disables the member counter.",
        required=False,
        max_length=25,
    )
    name_template = discord.ui.TextInput(
        label="Category Name",
        placeholder="members: {counter}!",
        required=True,
        max_length=100,
    )

    def __init__(self, panel: SettingsPanel):
        super().__init__()
        self.panel = panel
        settings = self.panel.store.member_counter()
        category_id = settings.get("category_id")
        self.category_id.default = str(category_id) if category_id else ""
        self.name_template.default = settings.get("name_template") or "members: {counter}!"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            raw_category_id = str(self.category_id.value).strip()
            category_id = parse_int_id(raw_category_id, "Category ID") if raw_category_id else None
            template = str(self.name_template.value).strip()
            if not template:
                raise ValueError("Category name cannot be empty.")
            if "{counter}" not in template:
                raise ValueError("Category name must include {counter}.")
            if len(template.replace("{counter}", "000000")) > 100:
                raise ValueError("Category name is too long.")
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        self.panel.store.set_member_counter(category_id, template)
        await self.panel.refresh_after_update(interaction.guild)
        await interaction.followup.send("Member counter updated.", ephemeral=True)


class WelcomeModal(SettingsModal, title="Welcome Settings"):
    channel_id = discord.ui.TextInput(
        label="Welcome Channel ID",
        placeholder="Blank disables welcome messages.",
        required=False,
        max_length=25,
    )
    message = discord.ui.TextInput(
        label="Welcome Message",
        placeholder="Welcome {mention}!",
        style=discord.TextStyle.long,
        required=True,
        max_length=1800,
    )

    def __init__(self, panel: SettingsPanel):
        super().__init__()
        self.panel = panel
        settings = self.panel.store.welcome()
        channel_id = settings.get("channel_id")
        self.channel_id.default = str(channel_id) if channel_id else ""
        self.message.default = settings.get("message") or "Welcome {mention}!"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            raw_channel_id = str(self.channel_id.value).strip()
            channel_id = parse_int_id(raw_channel_id, "Welcome Channel ID") if raw_channel_id else None
            message = str(self.message.value).strip()
            if channel_id and not message:
                raise ValueError("Welcome message cannot be empty.")
            if not message:
                message = "Welcome {mention}!"
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        self.panel.store.set_welcome(channel_id, message)
        await self.panel.refresh_after_update(interaction.guild)
        await interaction.followup.send("Welcome settings updated.", ephemeral=True)


class SuggestionModal(Settings Modal, title="Suggestion"):
    suggestion = discord.ui.TextInput(
        label="Suggestion",
        placeholder="Type your suggestion here.",
        style=discord.TextStyle.long,
        required=True,
        max_length=1800,
    )

    def __init__(self, panel: SettingsPanel):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            suggestion = clean_suggestion(str(self.suggestion.value))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        save_suggestion(interaction, self.panel.store, suggestion)
        await interaction.followup.send("Suggestion saved.", ephemeral=True)
