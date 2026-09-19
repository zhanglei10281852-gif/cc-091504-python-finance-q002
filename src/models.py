"""领域模型：家庭成员、目标、账户（税务属性/批次/待交收）、定投与临时支出。

金额一律使用 Decimal（字符串构造），序列化为两位小数字符串，
避免浮点误差进入税务与缺口计算。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

TWO = Decimal("0.01")
ZERO = Decimal("0")
GOAL_TYPES = ("emergency", "housing", "education", "retirement")
RISK_BANDS = ("conservative", "balanced", "growth")
TAX_STATUSES = ("taxable", "tax_deferred", "tax_exempt",
                "housing_special", "education_special")
FREQUENCIES = ("monthly", "quarterly", "annually", "one_off")


def D(value: Any) -> Decimal:
    """安全构造金额 Decimal。"""
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(str(value))


def cents(value: Decimal) -> str:
    q = value.quantize(TWO, rounding=ROUND_HALF_UP)
    if q.is_zero():
        q = abs(q)  # 归一化 -0.00，同时保留两位小数
    return str(q)


def parse_date(value: str | None) -> date | None:
    if value in (None, ""):
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def d_str(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _req(obj: dict, key: str, ctx: str) -> Any:
    if key not in obj:
        raise ValueError(f"{ctx} 缺少必填字段: {key}")
    return obj[key]


@dataclass
class Member:
    id: str
    name: str
    relation: str
    birth_date: date | None

    @classmethod
    def from_dict(cls, obj: dict) -> "Member":
        return cls(obj["id"], obj.get("name", obj["id"]),
                   obj.get("relation", ""), parse_date(obj.get("birth_date")))

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "relation": self.relation,
                "birth_date": d_str(self.birth_date)}


@dataclass
class Goal:
    id: str
    type: str
    name: str
    member_id: str | None
    target_amount: Decimal
    target_date: date | None
    priority: int
    risk_band: str

    def __post_init__(self) -> None:
        if self.type not in GOAL_TYPES:
            raise ValueError(f"目标 {self.id} 类型非法: {self.type}")
        if self.risk_band not in RISK_BANDS:
            raise ValueError(f"目标 {self.id} 风险区间非法: {self.risk_band}")
        if self.type != "emergency" and self.target_date is None:
            raise ValueError(f"非应急目标 {self.id} 必须有目标日期")
        if self.target_amount <= 0:
            raise ValueError(f"目标 {self.id} 金额必须为正")

    @classmethod
    def from_dict(cls, obj: dict) -> "Goal":
        gtype = _req(obj, "type", f"目标 {obj.get('id')}")
        return cls(
            id=_req(obj, "id", "目标"),
            type=gtype,
            name=obj.get("name", obj["id"]),
            member_id=obj.get("member_id"),
            target_amount=D(obj.get("target_amount")),
            target_date=parse_date(obj.get("target_date")),
            priority=int(obj.get("priority", 100)),
            risk_band=obj.get("risk_band", "balanced"),
        )

    def to_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "name": self.name,
                "member_id": self.member_id,
                "target_amount": cents(self.target_amount),
                "target_date": d_str(self.target_date),
                "priority": self.priority, "risk_band": self.risk_band}


@dataclass
class Lot:
    id: str
    instrument_id: str
    units: Decimal
    cost_basis: Decimal
    acquisition_date: date
    locked: bool = False                # 客户锁定：任何调仓都不得绕过
    lockup_until: date | None = None    # 禁售期：到期日前不可卖出
    note: str = ""

    @classmethod
    def from_dict(cls, obj: dict) -> "Lot":
        return cls(
            id=_req(obj, "id", "持仓批次"),
            instrument_id=_req(obj, "instrument_id", f"批次 {obj.get('id')}"),
            units=D(obj.get("units")),
            cost_basis=D(obj.get("cost_basis")),
            acquisition_date=parse_date(obj.get("acquisition_date")) or date.today(),
            locked=bool(obj.get("locked", False)),
            lockup_until=parse_date(obj.get("lockup_until")),
            note=obj.get("note", ""),
        )

    def to_dict(self) -> dict:
        return {"id": self.id, "instrument_id": self.instrument_id,
                "units": str(self.units), "cost_basis": cents(self.cost_basis),
                "acquisition_date": d_str(self.acquisition_date),
                "locked": self.locked, "lockup_until": d_str(self.lockup_until),
                "note": self.note}


@dataclass
class UnsettledCash:
    amount: Decimal
    settlement_date: date
    source: str = ""

    @classmethod
    def from_dict(cls, obj: dict) -> "UnsettledCash":
        return cls(D(_req(obj, "amount", "待交收资金")),
                   parse_date(obj.get("settlement_date")) or date.today(),
                   obj.get("source", ""))

    def to_dict(self) -> dict:
        return {"amount": cents(self.amount),
                "settlement_date": d_str(self.settlement_date), "source": self.source}


@dataclass
class Account:
    id: str
    name: str
    institution: str
    tax_status: str
    goal_ids: list[str]
    cash: Decimal
    unsettled_cash: list[UnsettledCash] = field(default_factory=list)
    lots: list[Lot] = field(default_factory=list)
    restricted_withdrawal: bool = False  # 如养老金账户：不得作为其他目标的变现捐赠方

    def __post_init__(self) -> None:
        if self.tax_status not in TAX_STATUSES:
            raise ValueError(f"账户 {self.id} 税务属性非法: {self.tax_status}")

    @classmethod
    def from_dict(cls, obj: dict) -> "Account":
        return cls(
            id=_req(obj, "id", "账户"),
            name=obj.get("name", obj["id"]),
            institution=obj.get("institution", ""),
            tax_status=_req(obj, "tax_status", f"账户 {obj.get('id')}"),
            goal_ids=list(obj.get("goal_ids", [])),
            cash=D(obj.get("cash")),
            unsettled_cash=[UnsettledCash.from_dict(x)
                            for x in obj.get("unsettled_cash", [])],
            lots=[Lot.from_dict(x) for x in obj.get("lots", [])],
            restricted_withdrawal=bool(obj.get("restricted_withdrawal", False)),
        )

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "institution": self.institution,
                "tax_status": self.tax_status, "goal_ids": list(self.goal_ids),
                "cash": cents(self.cash),
                "unsettled_cash": [x.to_dict() for x in self.unsettled_cash],
                "lots": [x.to_dict() for x in self.lots],
                "restricted_withdrawal": self.restricted_withdrawal}

    # ---- 便捷聚合 ----
    def market_value(self, prices: dict[str, Decimal]) -> Decimal:
        total = self.cash
        for lot in self.lots:
            total += lot.units * prices[lot.instrument_id]
        return total

    def settled_cash_by(self, on: date) -> Decimal:
        """截至 on 日已交收的现金（含到期待交收）。"""
        total = self.cash
        total += sum((u.amount for u in self.unsettled_cash
                      if u.settlement_date <= on), ZERO)
        return total


@dataclass
class Contribution:
    id: str
    goal_id: str
    account_id: str
    amount: Decimal
    frequency: str
    anchor_day: int
    start_date: date
    end_date: date | None

    def __post_init__(self) -> None:
        if self.frequency not in FREQUENCIES:
            raise ValueError(f"定投 {self.id} 频率非法: {self.frequency}")

    @classmethod
    def from_dict(cls, obj: dict) -> "Contribution":
        return cls(
            id=_req(obj, "id", "定投"),
            goal_id=_req(obj, "goal_id", f"定投 {obj.get('id')}"),
            account_id=_req(obj, "account_id", f"定投 {obj.get('id')}"),
            amount=D(obj.get("amount")),
            frequency=obj.get("frequency", "monthly"),
            anchor_day=int(obj.get("anchor_day", 1)),
            start_date=parse_date(obj.get("start_date")) or date.today(),
            end_date=parse_date(obj.get("end_date")),
        )

    def to_dict(self) -> dict:
        return {"id": self.id, "goal_id": self.goal_id,
                "account_id": self.account_id, "amount": cents(self.amount),
                "frequency": self.frequency, "anchor_day": self.anchor_day,
                "start_date": d_str(self.start_date), "end_date": d_str(self.end_date)}

    def scheduled_dates(self, upto: date) -> list[date]:
        """返回 (start, min(end, upto)] 语义下锚点日落入区间的计划日期（含 start）。"""
        dates: list[date] = []
        end = self.end_date if self.end_date and self.end_date < upto else upto
        if self.frequency == "one_off":
            if self.start_date <= end:
                dates.append(self.start_date)
            return dates
        months_step = {"monthly": 1, "quarterly": 3, "annually": 12}[self.frequency]
        y, m = self.start_date.year, self.start_date.month
        while True:
            d = date(y, m, min(self.anchor_day, 28))
            if d > end:
                break
            if d >= self.start_date:
                dates.append(d)
            m += months_step
            while m > 12:
                m -= 12
                y += 1
        return dates


@dataclass
class Expense:
    id: str
    name: str
    goal_id: str | None
    account_id: str
    amount: Decimal
    due_date: date

    @classmethod
    def from_dict(cls, obj: dict) -> "Expense":
        return cls(
            id=_req(obj, "id", "临时支出"),
            name=obj.get("name", obj["id"]),
            goal_id=obj.get("goal_id"),
            account_id=_req(obj, "account_id", f"支出 {obj.get('id')}"),
            amount=D(obj.get("amount")),
            due_date=parse_date(obj.get("due_date")) or date.today(),
        )

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "goal_id": self.goal_id,
                "account_id": self.account_id, "amount": cents(self.amount),
                "due_date": d_str(self.due_date)}


@dataclass
class Household:
    id: str
    name: str
    base_currency: str
    members: list[Member]

    @classmethod
    def from_dict(cls, obj: dict) -> "Household":
        cur = obj.get("base_currency", "CNY")
        return cls(obj["id"], obj.get("name", obj["id"]), cur,
                   [Member.from_dict(m) for m in obj.get("members", [])])

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name,
                "base_currency": self.base_currency,
                "members": [m.to_dict() for m in self.members]}


@dataclass
class Assumptions:
    monthly_household_expense: Decimal = ZERO
    emergency_months: int = 6
    default_risk_band: str = "balanced"
    housing_target_date_original: date | None = None
    note: str = ""
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, obj: dict | None) -> "Assumptions":
        obj = obj or {}
        known = {"monthly_household_expense", "emergency_months",
                 "default_risk_band", "housing_target_date_original", "note"}
        band = obj.get("default_risk_band", "balanced")
        if band not in RISK_BANDS:
            raise ValueError(f"默认风险区间非法: {band}")
        extra = {k: v for k, v in obj.items() if k not in known}
        return cls(
            monthly_household_expense=D(obj.get("monthly_household_expense")),
            emergency_months=int(obj.get("emergency_months", 6)),
            default_risk_band=band,
            housing_target_date_original=parse_date(
                obj.get("housing_target_date_original")),
            note=obj.get("note", ""),
            extra=extra,
        )

    def to_dict(self) -> dict:
        out = {"monthly_household_expense": cents(self.monthly_household_expense),
               "emergency_months": self.emergency_months,
               "default_risk_band": self.default_risk_band,
               "housing_target_date_original": d_str(
                   self.housing_target_date_original),
               "note": self.note}
        out.update(self.extra)
        return out


@dataclass
class Plan:
    id: str
    household: Household
    as_of: date
    assumptions: Assumptions
    goals: list[Goal]
    accounts: list[Account]
    contributions: list[Contribution]
    expenses: list[Expense]

    @classmethod
    def from_dict(cls, obj: dict) -> "Plan":
        as_of = parse_date(obj.get("as_of")) or date.today()
        goals = [Goal.from_dict(x) for x in obj.get("goals", [])]
        accounts = [Account.from_dict(x) for x in obj.get("accounts", [])]
        contributions = [Contribution.from_dict(x)
                         for x in obj.get("contributions", [])]
        expenses = [Expense.from_dict(x) for x in obj.get("expenses", [])]
        plan = cls(
            id=_req(obj, "id", "方案"),
            household=Household.from_dict(_req(obj, "household", "方案")),
            as_of=as_of,
            assumptions=Assumptions.from_dict(obj.get("assumptions")),
            goals=goals, accounts=accounts,
            contributions=contributions, expenses=expenses,
        )
        plan.validate_refs()
        return plan

    def validate_refs(self) -> None:
        gids = {g.id for g in self.goals}
        acks = {a.id for a in self.accounts}
        for a in self.accounts:
            for gid in a.goal_ids:
                if gid not in gids:
                    raise ValueError(f"账户 {a.id} 关联了不存在的目标 {gid}")
        for c in self.contributions:
            if c.goal_id not in gids:
                raise ValueError(f"定投 {c.id} 引用不存在的目标 {c.goal_id}")
            if c.account_id not in acks:
                raise ValueError(f"定投 {c.id} 引用不存在的账户 {c.account_id}")
        for e in self.expenses:
            if e.goal_id and e.goal_id not in gids:
                raise ValueError(f"支出 {e.id} 引用不存在的目标 {e.goal_id}")
            if e.account_id not in acks:
                raise ValueError(f"支出 {e.id} 引用不存在的账户 {e.account_id}")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "household": self.household.to_dict(),
            "as_of": d_str(self.as_of),
            "assumptions": self.assumptions.to_dict(),
            "goals": [g.to_dict() for g in self.goals],
            "accounts": [a.to_dict() for a in self.accounts],
            "contributions": [c.to_dict() for c in self.contributions],
            "expenses": [e.to_dict() for e in self.expenses],
        }

    # ---- 查找 ----
    def goal(self, gid: str) -> Goal:
        return next(g for g in self.goals if g.id == gid)

    def account(self, aid: str) -> Account:
        return next(a for a in self.accounts if a.id == aid)

    def accounts_for(self, gid: str) -> list[Account]:
        return [a for a in self.accounts if gid in a.goal_ids]

    def holdings_by_goal_class(self, prices: dict[str, Decimal],
                               instrument_class: dict[str, str]
                               ) -> dict[str, dict[str, Decimal]]:
        """goal_id -> asset_class -> 市值（按账户的目标映射归集）。"""
        out: dict[str, dict[str, Decimal]] = {}
        for account in self.accounts:
            for gid in account.goal_ids:
                rows = out.setdefault(gid, {})
                rows["cash"] = rows.get("cash", ZERO) + account.cash
                for lot in account.lots:
                    klass = instrument_class[lot.instrument_id]
                    rows[klass] = rows.get(klass, ZERO) + lot.units * prices[lot.instrument_id]
                for u in account.unsettled_cash:
                    rows["cash_pending"] = rows.get("cash_pending", ZERO) + u.amount
        return out
