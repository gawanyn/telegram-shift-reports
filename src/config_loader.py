from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Dict, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


@dataclass
class ChatGroup:
    id: str
    title: str
    env_var: str
    chat_id: int | str | None = None


@dataclass
class Person:
    group: str
    display_name: str
    username: Optional[str] = None
    telegram_id: Optional[int] = None
    branch_code: Optional[str] = None
    is_active: bool = True


@dataclass
class Branch:
    group: str
    code: str
    name: str
    work_weekdays: list[int]


@dataclass
class AppConfig:
    timezone: str
    schedule_enabled: bool
    branch_calendar_enabled: bool
    reminder_time: str
    missing_check_time: str
    dm_time: str
    followup_delay_min_sec: int
    followup_delay_max_sec: int
    groups: list[ChatGroup]
    branches: dict[tuple[str, str], Branch]
    people: list[Person]
    messages: dict[str, Any]

    def people_for_group(self, group_id: str) -> list[Person]:
        return [p for p in self.people if p.group == group_id]

    def branches_for_group(self, group_id: str) -> dict[str, Branch]:
        return {
            code: branch
            for (grp, code), branch in self.branches.items()
            if grp == group_id
        }

    def group_by_id(self, group_id: str) -> ChatGroup | None:
        for group in self.groups:
            if group.id == group_id:
                return group
        return None

    def message_for_group(self, group_id: str, key: str, default: str = "") -> str:
        by_group = self.messages.get("by_group", {})
        group_msgs = by_group.get(group_id, {})
        if key in group_msgs and group_msgs[key]:
            return str(group_msgs[key]).strip()
        value = self.messages.get(key, default)
        return str(value).strip() if value else default


def _normalize_username(raw: str | None) -> str | None:
    if not raw:
        return None
    name = str(raw).strip().lstrip("@")
    return name or None


def _load_yaml(name: str) -> dict[str, Any]:
    with (CONFIG_DIR / name).open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _parse_chat_id(raw: str) -> int | str:
    raw = raw.strip()
    if raw.startswith("@"):
        return raw
    return int(raw)


def load_config() -> AppConfig:
    from .env_setup import load_env

    load_env()
    settings = _load_yaml("settings.yaml")
    groups_raw = _load_yaml("groups.yaml").get("groups", [])
    branches_raw = _load_yaml("branches.yaml").get("branches", [])
    people_raw = _load_yaml("people.yaml").get("people", [])
    messages = _load_yaml("messages.yaml")

    groups: list[ChatGroup] = []
    for item in groups_raw:
        env_var = item["env_var"]
        raw = os.environ.get(env_var, "").strip()
        chat_id = _parse_chat_id(raw) if raw else None
        groups.append(
            ChatGroup(
                id=str(item["id"]),
                title=item["title"],
                env_var=env_var,
                chat_id=chat_id,
            )
        )

    branches: dict[tuple[str, str], Branch] = {}
    for item in branches_raw:
        group = str(item["group"])
        branch = Branch(
            group=group,
            code=str(item["code"]),
            name=item["name"],
            work_weekdays=list(item["work_weekdays"]),
        )
        key = (group, branch.code)
        if key in branches:
            raise ValueError(f"Дубль відділення: {group} / {branch.code}")
        branches[key] = branch

    people: list[Person] = []
    for item in people_raw:
        tid = item.get("telegram_id")
        people.append(
            Person(
                group=str(item["group"]),
                display_name=item["display_name"],
                username=_normalize_username(item.get("username")),
                telegram_id=int(tid) if tid is not None else None,
                branch_code=str(item["branch_code"]),
                is_active=item.get("is_active", True)
            )
        )

    return AppConfig(
        timezone=settings.get("timezone", "Europe/Kyiv"),
        schedule_enabled=bool(settings.get("schedule_enabled", False)),
        branch_calendar_enabled=bool(settings.get("branch_calendar_enabled", False)),
        reminder_time=settings.get("reminder_time", "17:00"),
        missing_check_time=settings["missing_check_time"],
        dm_time=settings["dm_time"],
        followup_delay_min_sec=int(settings.get("followup_delay_min_sec", 180)),
        followup_delay_max_sec=int(settings.get("followup_delay_max_sec", 480)),
        groups=groups,
        branches=branches,
        people=people,
        messages=messages,
    )
