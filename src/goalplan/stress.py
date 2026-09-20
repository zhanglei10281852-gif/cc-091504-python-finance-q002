"""压力情景：方案（调仓后）在各情景下到期达成情况。

做法：把建议视为已在计划交易日成交，得到调仓后持仓；
对每个情景按风险带施加即时冲击，再按资产预期收益滚动到各目标
到期日，叠加定投终值、扣减临时支出，得到到期达成率。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .engine import RebalancePlan
from .models import D, Goal, PlanInput, RiskBand, Side, money

# 示例压力情景（风险带 -> 即时冲击幅度）
DEFAULT_SCENARIOS: dict[str, dict[str, float]] = {
    "baseline": {},
    "equity_crash": {
        "equity": -0.35, "balanced": -0.20, "fixed_income": -0.03, "cash": 0.0,
    },
    "rate_shock": {
        "equity": -0.10, "balanced": -0.06, "fixed_income": -0.08, "cash": 0.0,
    },
    "stagflation": {
        "equity": -0.25, "balanced": -0.15, "fixed_income": -0.05, "cash": -0.02,
    },
}


@dataclass
class _Position:
    account_id: str
    asset_class: str
    value: Decimal


def _post_plan_positions(inp: PlanInput, plan: RebalancePlan) -> tuple[list[_Position], Decimal]:
    """调仓后持仓：卖出按批次扣减、买入生成新持仓，现金按净额调整。"""
    sold_qty: dict[str, Decimal] = {}
    buys: list[_Position] = []
    cash_delta = Decimal("0")
    for rec in plan.recommendations:
        cash_delta += rec.net_cash_delta
        if rec.side == Side.SELL and rec.lot_id:
            sold_qty[rec.lot_id] = sold_qty.get(rec.lot_id, Decimal("0")) + rec.quantity
        elif rec.side == Side.BUY:
            buys.append(_Position(rec.account_id, rec.asset_class, rec.gross_amount))

    positions: list[_Position] = []
    for lot in inp.lots:
        remaining_qty = lot.quantity - sold_qty.get(lot.id, Decimal("0"))
        if remaining_qty > 0:
            positions.append(
                _Position(lot.account_id, lot.asset_class, money(remaining_qty * lot.unit_price))
            )
    positions.extend(buys)
    cash = sum((a.cash_balance for a in inp.accounts), Decimal("0")) + cash_delta
    return positions, money(cash)


def _recurring_fv(inp: PlanInput, goal: Goal, as_of) -> Decimal:
    """定投在目标到期日的终值（与引擎口径一致）。"""
    total = Decimal("0")
    funding = set(goal.funding_account_ids) or {a.id for a in inp.accounts}
    for rec in inp.recurring_investments:
        if rec.goal_id and rec.goal_id != goal.id:
            continue
        if not rec.goal_id and rec.account_id not in funding:
            continue
        start = rec.next_date or as_of
        if start >= goal.target_date:
            continue
        months = (goal.target_date - start).days / 30.4375
        n = int(months // rec.interval_months)
        if n <= 0:
            continue
        asset = inp.asset_classes.get(rec.asset_class)
        er = asset.expected_return if asset else 0.0
        i = (1 + er) ** (rec.interval_months / 12) - 1
        fv = float(rec.amount) * n if i <= 0 else float(rec.amount) * (((1 + i) ** n - 1) / i)
        total += D(fv)
    return total


def _temp_expenses(inp: PlanInput, goal: Goal, as_of) -> Decimal:
    """与引擎口径一致：仅计入显式归属该目标的临时支出。"""
    total = Decimal("0")
    for exp in inp.temporary_expenses:
        if exp.goal_id != goal.id:
            continue
        if as_of <= exp.date <= goal.target_date:
            total += exp.amount
    return total


def stress_test(
    inp: PlanInput,
    plan: RebalancePlan,
    scenarios: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """对方案执行后的组合做压力测算，返回各情景下每个目标的到期达成情况。"""
    scenarios = scenarios or DEFAULT_SCENARIOS
    positions, cash = _post_plan_positions(inp, plan)
    account_ids = {a.id for a in inp.accounts}
    # 方案为近期目标预留的现金计入对应目标；应急储备不归属任何目标
    reserved: dict[str, float] = plan.cash_projection.get("reserved_for_goals", {})
    emergency_target = float(plan.cash_projection.get("emergency_target", 0))
    shareable = max(0.0, float(cash) - sum(reserved.values()) - emergency_target)

    out: dict[str, Any] = {"as_of": plan.as_of.isoformat(), "scenarios": []}
    for name, shocks in scenarios.items():
        goals_out = []
        for goal in inp.goals:
            funding = set(goal.funding_account_ids) or account_ids
            years = max(0.0, (goal.target_date - plan.as_of).days / 365.25)
            value = Decimal("0")
            for pos in positions:
                if pos.account_id not in funding:
                    continue
                asset = inp.asset_classes.get(pos.asset_class)
                band = asset.risk_band if asset else RiskBand.CASH
                er = asset.expected_return if asset else 0.0
                shock = shocks.get(band.value, 0.0)
                value += D(float(pos.value) * (1 + shock) * (1 + er) ** years)
            cash_shock = shocks.get("cash", 0.0)
            cash_growth = (1 + cash_shock) * (1 + inp.assumptions.cash_expected_return) ** years
            # 为该目标预留的现金
            if goal.id in reserved:
                value += D(reserved[goal.id] * cash_growth)
            # 未指定归属账户的目标共享应急与预留之外的自由现金
            if not goal.funding_account_ids:
                value += D(shareable * cash_growth)
            value += _recurring_fv(inp, goal, plan.as_of)
            value -= _temp_expenses(inp, goal, plan.as_of)
            ratio = float(value / goal.target_amount) if goal.target_amount > 0 else 1.0
            goals_out.append({
                "goal_id": goal.id,
                "name": goal.name,
                "target_date": goal.target_date.isoformat(),
                "target_amount": float(goal.target_amount),
                "projected_value": float(money(value)),
                "achievement_ratio": round(ratio, 4),
                "met": value >= goal.target_amount,
            })
        out["scenarios"].append({
            "name": name,
            "shocks": shocks,
            "goals": goals_out,
            "all_met": all(g["met"] for g in goals_out),
        })
    return out
