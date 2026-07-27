import copy
import json
import re
from datetime import UTC, datetime
from datetime import timedelta
from pathlib import Path
from typing import Any


SETTINGS_CHANNEL_ID = 1531073727112941688
MANAGER_ROLE_ID = 1530711866890784879
DEFAULT_WELCOME_CHANNEL_ID = 1530934402589392987

DEFAULT_SETTINGS = {
    "settings_panel_message_id": None,
    "auto_delete_rules": [],
    "blacklisted_words": ["nigger", "nigga", "faggot", "fag"],
    "member_counter": {
        "category_id": None,
        "name_template": "members: {counter}!",
    },
    "welcome": {
        "channel_id": DEFAULT_WELCOME_CHANNEL_ID,
        "message": "welcome to necro's server {mention}",
    },
    "suggestions": [],
    "auto_responses": [],
}

TIME_VALUE_RE = re.compile(r"^(\d+)([hdw])$", re.IGNORECASE)


def _deep_merge(defaults: dict[str, Any], loaded: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(defaults)
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def parse_int_id(value: str, field_name: str) -> int:
    cleaned = value.strip()
    if not cleaned.isdigit():
        raise ValueError(f"{field_name} must be a numeric Discord ID.")
    return int(cleaned)


def normalize_time_value(value: str | None) -> str | None:
    if value is None:
        return None

    cleaned = value.strip().lower()
    if not cleaned:
        return None

    match = TIME_VALUE_RE.fullmatch(cleaned)
    if not match:
        raise ValueError("Time must look like 1h, 5d, 1w, or 2w.")

    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "h" and 1 <= amount <= 23:
        return f"{amount}h"
    if unit == "d" and 1 <= amount <= 6:
        return f"{amount}d"
    if unit == "w" and 1 <= amount <= 2:
        return f"{amount}w"

    raise ValueError("Allowed times are 1-23h, 1-6d, 1w, or 2w.")


def duration_from_time_value(value: str) -> timedelta:
    normalized = normalize_time_value(value)
    if not normalized:
        raise ValueError("Time value is empty.")

    amount = int(normalized[:-1])
    unit = normalized[-1]
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "w":
        return timedelta(weeks=amount)
    raise ValueError("Invalid time value.")


def parse_amount_value(value: str | None) -> int | None:
    if value is None:
        return None

    cleaned = value.strip()
    if not cleaned:
        return None
    if not cleaned.isdigit():
        raise ValueError("Amount must be a positive number.")

    amount = int(cleaned)
    if amount < 1:
        raise ValueError("Amount must be at least 1.")
    return amount


def parse_blacklisted_words(value: str) -> list[str]:
    words = []
    seen = set()
    for part in re.split(r"[,\n]+", value):
        word = part.strip().lower()
        if not word or word in seen:
            continue
        seen.add(word)
        words.append(word)
    return words


def normalize_keyword(value: str) -> str:
    keyword = value.strip().lower()
    if not keyword:
        raise ValueError("Keyword cannot be empty.")
    if len(keyword) > 100:
        raise ValueError("Keyword must be 100 characters or less.")
    return keyword


class SettingsStore:
    def __init__(self, path: str | Path = "bot_settings.json"):
        self.path = Path(path)
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return copy.deepcopy(DEFAULT_SETTINGS)

        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if not isinstance(loaded, dict):
            loaded = {}
        return _deep_merge(DEFAULT_SETTINGS, loaded)

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self._data, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def settings_panel_message_id(self) -> int | None:
        value = self._data.get("settings_panel_message_id")
        return int(value) if value else None

    def set_settings_panel_message_id(self, message_id: int) -> None:
        self._data["settings_panel_message_id"] = int(message_id)
        self.save()

    def auto_delete_rules(self) -> list[dict[str, Any]]:
        return list(self._data.get("auto_delete_rules", []))

    def upsert_auto_delete_rule(
        self,
        channel_id: int,
        time_value: str | None,
        amount: int | None,
    ) -> str:
        rules = [
            rule
            for rule in self.auto_delete_rules()
            if int(rule.get("channel_id", 0)) != channel_id
        ]

        if time_value is None and amount is None:
            self._data["auto_delete_rules"] = rules
            self.save()
            return "removed"

        rule: dict[str, Any] = {"channel_id": channel_id}
        if time_value is not None:
            rule["time"] = time_value
        if amount is not None:
            rule["amount"] = amount
        rules.append(rule)
        self._data["auto_delete_rules"] = rules
        self.save()
        return "saved"

    def blacklisted_words(self) -> list[str]:
        return list(self._data.get("blacklisted_words", []))

    def set_blacklisted_words(self, words: list[str]) -> None:
        self._data["blacklisted_words"] = words
        self.save()

    def member_counter(self) -> dict[str, Any]:
        return dict(self._data.get("member_counter", {}))

    def set_member_counter(self, category_id: int | None, name_template: str) -> None:
        self._data["member_counter"] = {
            "category_id": category_id,
            "name_template": name_template,
        }
        self.save()

    def welcome(self) -> dict[str, Any]:
        return dict(self._data.get("welcome", {}))

    def set_welcome(self, channel_id: int | None, message: str) -> None:
        self._data["welcome"] = {
            "channel_id": channel_id,
            "message": message,
        }
        self.save()

    def suggestions(self) -> list[dict[str, Any]]:
        return list(self._data.get("suggestions", []))

    def add_suggestion(
        self,
        author_id: int,
        author_name: str,
        suggestion: str,
        guild_id: int | None,
        channel_id: int | None,
    ) -> None:
        suggestions = self.suggestions()
        suggestions.append(
            {
                "author_id": int(author_id),
                "author_name": author_name,
                "guild_id": int(guild_id) if guild_id else None,
                "channel_id": int(channel_id) if channel_id else None,
                "suggestion": suggestion,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        self._data["suggestions"] = suggestions
        self.save()

    def auto_responses(self) -> list[dict[str, str]]:
        return list(self._data.get("auto_responses", []))

    def upsert_auto_response(self, keyword: str, response: str | None) -> str:
        normalized_keyword = normalize_keyword(keyword)
        rules = []
        for rule in self.auto_responses():
            try:
                existing_keyword = normalize_keyword(str(rule.get("keyword", "")))
            except ValueError:
                continue
            if existing_keyword != normalized_keyword:
                rules.append(rule)

        cleaned_response = (response or "").strip()
        if not cleaned_response:
            self._data["auto_responses"] = rules
            self.save()
            return "removed"

        if len(cleaned_response) > 1800:
            raise ValueError("Response must be 1800 characters or less.")

        rules.append({"keyword": normalized_keyword, "response": cleaned_response})
        self._data["auto_responses"] = rules
        self.save()
        return "saved"
