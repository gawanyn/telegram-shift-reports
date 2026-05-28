from __future__ import annotations

import re
from dataclasses import dataclass

REPORT_RE = re.compile(
    r"^\s*(\d+)\s*,\s*([^,]+)\s*,\s*"
    r"Виплачена\s+пенсія\s*[—–-]\s*(\d+)\s*,\s*"
    r"Торгівля\s*[—–-]\s*(\d+)\s*(?:грн)?\s*,\s*"
    r"Передплата\s*[—–-]\s*(\d+)\s*шт\.?\s*$",
    re.IGNORECASE,
)


@dataclass
class ParsedReport:
    branch_code: str
    branch_name: str
    pension_paid: int
    trade_uah: int
    prepayment_units: int

    @property
    def zero_trade(self) -> bool:
        return self.trade_uah == 0

    @property
    def zero_prepayment(self) -> bool:
        return self.prepayment_units == 0


def parse_report(text: str) -> ParsedReport | None:
    normalized = " ".join(text.split())
    match = REPORT_RE.match(normalized)
    if not match:
        return None
    code, name, pension, trade, prepayment = match.groups()
    return ParsedReport(
        branch_code=code.strip(),
        branch_name=name.strip(),
        pension_paid=int(pension),
        trade_uah=int(trade),
        prepayment_units=int(prepayment),
    )
