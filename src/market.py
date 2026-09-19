"""参考数据加载与交易日历。

reference/ 下均为样例数据：资产分类、工具（含最低交易单位/交收天数）、
费用税率、交易日历（节假日/周末）、压力情景。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from models import D, ZERO, parse_date

REFERENCE_DIR = Path(__file__).resolve().parents[1] / "reference"


@dataclass(frozen=True)
class Instrument:
    id: str
    name: str
    asset_class: str
    last_price: Decimal
    lot_size: int
    min_trade_amount: Decimal
    settlement_days: int


@dataclass(frozen=True)
class CgtRule:
    tax_status: str
    short_rate: Decimal
    long_rate: Decimal
    long_holding_days: int


@dataclass(frozen=True)
class FeeSchedule:
    commission_bp: Decimal
    min_commission: Decimal
    stamp_duty_bp_sell: Decimal
    cgt: dict[str, CgtRule]

    def cgt_rate(self, tax_status: str, holding_days: int) -> Decimal:
        rule = self.cgt.get(tax_status)
        if rule is None or (rule.short_rate == 0 and rule.long_rate == 0):
            return ZERO
        if holding_days >= rule.long_holding_days:
            return rule.long_rate
        return rule.short_rate


@dataclass(frozen=True)
class Scenario:
    id: str
    name: str
    returns: dict[str, Decimal]
    contribution_haircut: Decimal
    target_multiplier: Decimal


class MarketData:
    def __init__(self, ref_dir: Path = REFERENCE_DIR) -> None:
        self.ref_dir = ref_dir
        self._load_asset_classes()
        self._load_instruments()
        self._load_fees()
        self._load_calendar()
        self._load_stress()

    def _read(self, name: str) -> dict:
        with (self.ref_dir / name).open(encoding="utf-8") as fh:
            return json.load(fh)

    def _load_asset_classes(self) -> None:
        raw = self._read("asset_classes.json")
        self.currency = raw["base_currency"]
        self.classes: dict[str, dict] = {c["id"]: c for c in raw["classes"]}
        self.glide = raw["glide_path"]
        self.drift_band = D(self.glide["drift_band"])
        self.min_rebalance_amount = D(self.glide["min_rebalance_amount"])
        self.risk_band_mix: dict[str, dict[str, Decimal]] = {
            band: {k: D(v) for k, v in mix.items()}
            for band, mix in self.glide["risk_band_mix"].items()}

    def _load_instruments(self) -> None:
        raw = self._read("instruments.json")
        self.quote_date = parse_date(raw["quote_date"])
        self.instruments: dict[str, Instrument] = {}
        self.prices: dict[str, Decimal] = {}
        for item in raw["instruments"]:
            inst = Instrument(
                id=item["id"], name=item["name"],
                asset_class=item["asset_class"],
                last_price=D(item["last_price"]),
                lot_size=int(item["lot_size"]),
                min_trade_amount=D(item["min_trade_amount"]),
                settlement_days=int(item.get("settlement_days", 1)),
            )
            if inst.asset_class not in self.classes:
                raise ValueError(f"工具 {inst.id} 引用未知资产分类 {inst.asset_class}")
            self.instruments[inst.id] = inst
            self.prices[inst.id] = inst.last_price
        self.default_instrument_by_class = raw["default_instrument_by_class"]

    def _load_fees(self) -> None:
        raw = self._read("fees.json")
        cgt = {r["tax_status"]: CgtRule(
            r["tax_status"], D(r["short_rate"]), D(r["long_rate"]),
            int(r["long_holding_days"])) for r in raw["capital_gains_tax"]}
        self.fees = FeeSchedule(
            commission_bp=D(raw["commission_bp"]) / D(10000),
            min_commission=D(raw["min_commission"]),
            stamp_duty_bp_sell=D(raw["stamp_duty_bp_sell"]) / D(10000),
            cgt=cgt,
        )

    def _load_calendar(self) -> None:
        raw = self._read("calendar.json")
        self.holidays = {parse_date(d) for d in raw["holidays"]}
        self.half_days = {parse_date(d) for d in raw.get("half_days", [])}

    def _load_stress(self) -> None:
        raw = self._read("stress.json")
        self.scenarios: dict[str, Scenario] = {}
        for s in raw["scenarios"]:
            self.scenarios[s["id"]] = Scenario(
                s["id"], s["name"],
                {k: D(v) for k, v in s["returns"].items()},
                D(s.get("contribution_haircut", 0)),
                D(s.get("target_multiplier", 1)),
            )

    def instrument_class(self, instrument_id: str) -> str:
        return self.instruments[instrument_id].asset_class

    # ---- 交易日历 ----
    def is_trading_day(self, day: date) -> bool:
        if day.weekday() >= 5:
            return False
        return day not in self.holidays

    def is_open_on(self, day: date) -> bool:
        return self.is_trading_day(day)

    def next_trading_day(self, day: date) -> date:
        cur = day
        while not self.is_trading_day(cur):
            cur += timedelta(days=1)
        return cur

    def trading_days_between(self, start: date, end: date) -> list[date]:
        out, cur = [], start
        while cur <= end:
            if self.is_trading_day(cur):
                out.append(cur)
            cur += timedelta(days=1)
        return out

    def settlement_date(self, trade_day: date, instrument_id: str) -> date:
        inst = self.instruments[instrument_id]
        remaining, cur = inst.settlement_days, trade_day
        while remaining > 0:
            cur += timedelta(days=1)
            if self.is_trading_day(cur):
                remaining -= 1
        return cur


def load_seed_plan() -> dict:
    with (REFERENCE_DIR / "seed_plan.json").open(encoding="utf-8") as fh:
        return json.load(fh)
