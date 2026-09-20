"""领域模型与序列化。

所有金额内部使用 Decimal，序列化为 JSON 时保留两位小数；
日期使用 ISO 格式字符串。`from_dict` 负责把 API 负载或版本快照
还原为强类型对象，字段缺失时给出明确错误而不是静默默认值。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_DOWN
from typing import Any

CENT = Decimal("0.01")
QUANT = Decimal("0.0001")


def D(value: Any) -> Decimal:
    """把输入安全转换为 Decimal，避免 float 直接构造带来的尾差。"""
    if isinstance(value, Decimal):
        return value
    if value is None:
        raise ValueError("金额字段不能为空")
    return Decimal(str(value))


def money(value: Any) -> Decimal:
    return D(value).quantize(CENT, rounding=ROUND_DOWN)


def parse_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _opt_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    return parse_date(value)


class GoalType(str, enum.Enum):
    EMERGENCY = "emergency"
    HOUSING = "housing"
    EDUCATION = "education"
    RETIREMENT = "retirement"
    OTHER = "other"


class RiskBand(str, enum.Enum):
    CASH = "cash"
    FIXED_INCOME = "fixed_income"
    BALANCED = "balanced"
    EQUITY = "equity"


class TaxTreatment(str, enum.Enum):
    TAXABLE = "taxable"            # 应税账户：卖出盈利计提资本利得税
    TAX_DEFERRED = "tax_deferred"  # 递延账户：卖出当期不计税
    TAX_FREE = "tax_free"          # 免税账户


class LotStatus(str, enum.Enum):
    TRADABLE = "tradable"
    LOCKED = "locked"                          # 禁售批次，locked_until 前不可卖
    PENDING_SETTLEMENT = "pending_settlement"  # 待交收，settlement_date 前不可卖


class Side(str, enum.Enum):
    SELL = "sell"
    BUY = "buy"


# ---------------------------------------------------------------------------
# 静态参照数据：资产分类与费用样例
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetClass:
    code: str
    name: str
    risk_band: RiskBand
    expected_return: float      # 年化预期收益（用于到期测算）
    volatility: float           # 年化波动率（用于压力情景说明）
    min_trade_unit: Decimal     # 最低交易单位（份）
    fee_code: str = "standard"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "risk_band": self.risk_band.value,
            "expected_return": self.expected_return,
            "volatility": self.volatility,
            "min_trade_unit": float(self.min_trade_unit),
            "fee_code": self.fee_code,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AssetClass":
        return cls(
            code=d["code"],
            name=d.get("name", d["code"]),
            risk_band=RiskBand(d["risk_band"]),
            expected_return=float(d.get("expected_return", 0.0)),
            volatility=float(d.get("volatility", 0.0)),
            min_trade_unit=D(d.get("min_trade_unit", 1)),
            fee_code=d.get("fee_code", "standard"),
        )


@dataclass(frozen=True)
class FeeSchedule:
    """费用样例：佣金（有最低收费）与卖出印花税，均可按资产覆盖。"""

    code: str
    commission_rate: Decimal = Decimal("0")
    min_commission: Decimal = Decimal("0")
    stamp_duty_rate: Decimal = Decimal("0")  # 仅卖出收取

    def fee_for(self, side: Side, gross: Decimal) -> Decimal:
        commission = max(self.min_commission, gross * self.commission_rate)
        stamp = gross * self.stamp_duty_rate if side == Side.SELL else Decimal("0")
        return money(commission + stamp)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "commission_rate": float(self.commission_rate),
            "min_commission": float(self.min_commission),
            "stamp_duty_rate": float(self.stamp_duty_rate),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FeeSchedule":
        return cls(
            code=d["code"],
            commission_rate=D(d.get("commission_rate", 0)),
            min_commission=D(d.get("min_commission", 0)),
            stamp_duty_rate=D(d.get("stamp_duty_rate", 0)),
        )


# ---------------------------------------------------------------------------
# 家庭、账户与持仓
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FamilyMember:
    id: str
    name: str
    relation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "relation": self.relation}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FamilyMember":
        return cls(id=d["id"], name=d["name"], relation=d.get("relation", ""))


@dataclass(frozen=True)
class PendingCash:
    """账户层面的待交收资金（如已卖出未到账），settle_date 起可用。"""

    amount: Decimal
    settle_date: date
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "amount": float(self.amount),
            "settle_date": self.settle_date.isoformat(),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PendingCash":
        return cls(
            amount=money(d["amount"]),
            settle_date=parse_date(d["settle_date"]),
            note=d.get("note", ""),
        )


@dataclass(frozen=True)
class Account:
    id: str
    owner_id: str
    name: str
    tax_treatment: TaxTreatment
    cash_balance: Decimal = Decimal("0")          # 已交收可用现金
    pending_cash: tuple[PendingCash, ...] = ()    # 待交收资金

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "name": self.name,
            "tax_treatment": self.tax_treatment.value,
            "cash_balance": float(self.cash_balance),
            "pending_cash": [p.to_dict() for p in self.pending_cash],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Account":
        return cls(
            id=d["id"],
            owner_id=d.get("owner_id", ""),
            name=d.get("name", d["id"]),
            tax_treatment=TaxTreatment(d.get("tax_treatment", "taxable")),
            cash_balance=money(d.get("cash_balance", 0)),
            pending_cash=tuple(PendingCash.from_dict(p) for p in d.get("pending_cash", [])),
        )


@dataclass(frozen=True)
class HoldingLot:
    """持仓批次。禁售、待交收与客户锁定的批次不得被建议绕过。"""

    id: str
    account_id: str
    asset_class: str
    quantity: Decimal
    unit_price: Decimal
    cost_basis: Decimal           # 单位成本，用于应税账户利得测算
    status: LotStatus = LotStatus.TRADABLE
    locked_until: date | None = None
    settlement_date: date | None = None
    client_locked: bool = False   # 客户锁定资产：任何建议不得触碰

    @property
    def market_value(self) -> Decimal:
        return money(self.quantity * self.unit_price)

    def sellable(self, as_of: date) -> bool:
        if self.client_locked:
            return False
        if self.status == LotStatus.LOCKED:
            return self.locked_until is not None and as_of >= self.locked_until
        if self.status == LotStatus.PENDING_SETTLEMENT:
            return self.settlement_date is not None and as_of >= self.settlement_date
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "account_id": self.account_id,
            "asset_class": self.asset_class,
            "quantity": float(self.quantity),
            "unit_price": float(self.unit_price),
            "cost_basis": float(self.cost_basis),
            "status": self.status.value,
            "locked_until": self.locked_until.isoformat() if self.locked_until else None,
            "settlement_date": self.settlement_date.isoformat() if self.settlement_date else None,
            "client_locked": self.client_locked,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "HoldingLot":
        return cls(
            id=d["id"],
            account_id=d["account_id"],
            asset_class=d["asset_class"],
            quantity=D(d["quantity"]),
            unit_price=D(d["unit_price"]),
            cost_basis=D(d.get("cost_basis", d["unit_price"])),
            status=LotStatus(d.get("status", "tradable")),
            locked_until=_opt_date(d.get("locked_until")),
            settlement_date=_opt_date(d.get("settlement_date")),
            client_locked=bool(d.get("client_locked", False)),
        )


# ---------------------------------------------------------------------------
# 目标、定投与临时支出
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Goal:
    id: str
    name: str
    goal_type: GoalType
    target_amount: Decimal
    target_date: date
    priority: int = 3                          # 1 最高，数字越小越优先
    funding_account_ids: tuple[str, ...] = ()  # 为空表示全部账户共同支持

    def horizon_months(self, as_of: date) -> float:
        return max(0.0, (self.target_date - as_of).days / 30.4375)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "goal_type": self.goal_type.value,
            "target_amount": float(self.target_amount),
            "target_date": self.target_date.isoformat(),
            "priority": self.priority,
            "funding_account_ids": list(self.funding_account_ids),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Goal":
        return cls(
            id=d["id"],
            name=d.get("name", d["id"]),
            goal_type=GoalType(d.get("goal_type", "other")),
            target_amount=money(d["target_amount"]),
            target_date=parse_date(d["target_date"]),
            priority=int(d.get("priority", 3)),
            funding_account_ids=tuple(d.get("funding_account_ids", [])),
        )


@dataclass(frozen=True)
class RecurringInvestment:
    """定投：按固定周期投入指定资产，可指定归属目标。"""

    id: str
    account_id: str
    asset_class: str
    amount: Decimal
    interval_months: int = 1
    next_date: date | None = None
    goal_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "account_id": self.account_id,
            "asset_class": self.asset_class,
            "amount": float(self.amount),
            "interval_months": self.interval_months,
            "next_date": self.next_date.isoformat() if self.next_date else None,
            "goal_id": self.goal_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RecurringInvestment":
        return cls(
            id=d["id"],
            account_id=d["account_id"],
            asset_class=d["asset_class"],
            amount=money(d["amount"]),
            interval_months=int(d.get("interval_months", 1)),
            next_date=_opt_date(d.get("next_date")),
            goal_id=d.get("goal_id"),
        )


@dataclass(frozen=True)
class TemporaryExpense:
    """临时支出：在到期测算与可用现金中予以扣减。"""

    id: str
    amount: Decimal
    date: date
    description: str = ""
    account_id: str | None = None
    goal_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "amount": float(self.amount),
            "date": self.date.isoformat(),
            "description": self.description,
            "account_id": self.account_id,
            "goal_id": self.goal_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TemporaryExpense":
        return cls(
            id=d["id"],
            amount=money(d["amount"]),
            date=parse_date(d["date"]),
            description=d.get("description", ""),
            account_id=d.get("account_id"),
            goal_id=d.get("goal_id"),
        )


# ---------------------------------------------------------------------------
# 方案假设与整体输入
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanAssumptions:
    monthly_expenses: Decimal                       # 家庭月支出（应急金基数）
    emergency_months: int = 6                       # 应急现金覆盖月数
    near_term_months: int = 24                      # 近期目标窗口
    drift_band: float = 0.05                        # 长期配置漂移带（±）
    target_allocation: dict[str, float] = field(default_factory=dict)      # 风险带 -> 目标权重
    default_buy_assets: dict[str, str] = field(default_factory=dict)       # 风险带 -> 买入资产
    gains_tax_rates: dict[str, float] = field(default_factory=lambda: {"taxable": 0.20})
    cash_expected_return: float = 0.015             # 现金类年化（测算用）

    def to_dict(self) -> dict[str, Any]:
        return {
            "monthly_expenses": float(self.monthly_expenses),
            "emergency_months": self.emergency_months,
            "near_term_months": self.near_term_months,
            "drift_band": self.drift_band,
            "target_allocation": dict(self.target_allocation),
            "default_buy_assets": dict(self.default_buy_assets),
            "gains_tax_rates": dict(self.gains_tax_rates),
            "cash_expected_return": self.cash_expected_return,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlanAssumptions":
        return cls(
            monthly_expenses=money(d.get("monthly_expenses", 0)),
            emergency_months=int(d.get("emergency_months", 6)),
            near_term_months=int(d.get("near_term_months", 24)),
            drift_band=float(d.get("drift_band", 0.05)),
            target_allocation={k: float(v) for k, v in d.get("target_allocation", {}).items()},
            default_buy_assets=dict(d.get("default_buy_assets", {})),
            gains_tax_rates={k: float(v) for k, v in d.get("gains_tax_rates", {"taxable": 0.20}).items()},
            cash_expected_return=float(d.get("cash_expected_return", 0.015)),
        )


@dataclass(frozen=True)
class PlanInput:
    """一份方案的全部输入假设；版本快照即以此为准。"""

    members: tuple[FamilyMember, ...]
    accounts: tuple[Account, ...]
    lots: tuple[HoldingLot, ...]
    goals: tuple[Goal, ...]
    recurring_investments: tuple[RecurringInvestment, ...]
    temporary_expenses: tuple[TemporaryExpense, ...]
    asset_classes: dict[str, AssetClass]
    fee_schedules: dict[str, FeeSchedule]
    assumptions: PlanAssumptions

    def account_map(self) -> dict[str, Account]:
        return {a.id: a for a in self.accounts}

    def to_dict(self) -> dict[str, Any]:
        return {
            "members": [m.to_dict() for m in self.members],
            "accounts": [a.to_dict() for a in self.accounts],
            "lots": [lot.to_dict() for lot in self.lots],
            "goals": [g.to_dict() for g in self.goals],
            "recurring_investments": [r.to_dict() for r in self.recurring_investments],
            "temporary_expenses": [e.to_dict() for e in self.temporary_expenses],
            "asset_classes": [a.to_dict() for a in self.asset_classes.values()],
            "fee_schedules": [f.to_dict() for f in self.fee_schedules.values()],
            "assumptions": self.assumptions.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlanInput":
        assets = {a.code: a for a in (AssetClass.from_dict(x) for x in d.get("asset_classes", []))}
        fees = {f.code: f for f in (FeeSchedule.from_dict(x) for x in d.get("fee_schedules", []))}
        return cls(
            members=tuple(FamilyMember.from_dict(x) for x in d.get("members", [])),
            accounts=tuple(Account.from_dict(x) for x in d.get("accounts", [])),
            lots=tuple(HoldingLot.from_dict(x) for x in d.get("lots", [])),
            goals=tuple(Goal.from_dict(x) for x in d.get("goals", [])),
            recurring_investments=tuple(
                RecurringInvestment.from_dict(x) for x in d.get("recurring_investments", [])
            ),
            temporary_expenses=tuple(
                TemporaryExpense.from_dict(x) for x in d.get("temporary_expenses", [])
            ),
            asset_classes=assets,
            fee_schedules=fees,
            assumptions=PlanAssumptions.from_dict(d.get("assumptions", {})),
        )
