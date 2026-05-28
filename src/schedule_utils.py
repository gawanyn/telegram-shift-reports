from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config_loader import AppConfig, Branch, Person


def today_local(cfg: AppConfig, now: datetime | None = None) -> date:
    tz = ZoneInfo(cfg.timezone)
    return (now or datetime.now(tz)).date()


def branch_works_today(branch: Branch, day: date) -> bool:
    return day.weekday() in branch.work_weekdays


def expected_people_today(
    cfg: AppConfig,
    group_id: str,
    day: date | None = None,
) -> list[Person]:
    day = day or today_local(cfg)
    branches = cfg.branches_for_group(group_id)
    result: list[Person] = []
    for person in cfg.people_for_group(group_id):
        branch = branches.get(person.branch_code)
        if branch is None:
            continue
        if not cfg.branch_calendar_enabled or branch_works_today(branch, day):
            result.append(person)
    return result


def parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)
