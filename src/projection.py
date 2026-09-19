"""目标资金到位分析与压力情景到期测算（确定性模型）。

约定：
- 资产按账户的目标映射归集；多目标账户按目标数均摊。
- 待交收现金仅在交收日落入目标到期日之前时计入。
- 近期目标对债券价值计提流动性折扣，股票按期限线性确认，
  避免用波动资产的瞬时市值覆盖短期现金缺口。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from market import MarketData, Scenario
from models import D, ZERO, Plan, Goal, cents, d_str

YEARS_DAYS = Decimal("365.25")


@dataclass
class GoalAnalysis:
    goal_id: str
    goal_type: str
    name: str
    priority: int
    target_date: date | None
    horizon_years: str
    target_amount: Decimal
    assets_by_class: dict[str, Decimal]
    pending_cash: Decimal
    recognized_ready: Decimal
    bond_haircut: Decimal
    equity_recognized_fraction: str
    scheduled_contributions: Decimal
    contributions_fv: Decimal
    attributed_expenses: Decimal
    projected_maturity_value: Decimal
    gap: Decimal
    funded_ratio: str
    target_mix: dict[str, Decimal]
    constraints: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "goal_id": self.goal_id, "goal_type": self.goal_type,
            "name": self.name, "priority": self.priority,
            "target_date": d_str(self.target_date),
            "horizon_years": self.horizon_years,
            "target_amount": cents(self.target_amount),
            "assets_by_class": {k: cents(v) for k, v in self.assets_by_class.items()},
            "pending_cash": cents(self.pending_cash),
            "recognized_ready": cents(self.recognized_ready),
            "bond_haircut": cents(self.bond_haircut),
            "equity_recognized_fraction": self.equity_recognized_fraction,
            "scheduled_contributions": cents(self.scheduled_contributions),
            "contributions_fv": cents(self.contributions_fv),
            "attributed_expenses": cents(self.attributed_expenses),
            "projected_maturity_value": cents(self.projected_maturity_value),
            "gap": cents(self.gap),
            "funded_ratio": self.funded_ratio,
            "target_mix": {k: cents(v) for k, v in self.target_mix.items()},
            "constraints": self.constraints,
        }


def years_between(a: date, b: date | None) -> Decimal:
    if b is None:
        return ZERO
    return Decimal((b - a).days) / YEARS_DAYS


def equity_fraction_cap(years: Decimal, glide: dict, band_stock: Decimal) -> Decimal:
    """根据目标期限给出权益占比上限（滑行路径）。"""
    cap1 = D(glide["equity_cap_1y"])
    cap3 = D(glide["equity_cap_3y"])
    if years <= 1:
        return cap1
    if years <= 3:
        return cap1 + (cap3 - cap1) * (years - 1) / D(2)
    if years < D(glide["long_term_years"]):
        return cap3 + (band_stock - cap3) * (years - 3) / D(
            glide["long_term_years"] - 3)
    return band_stock


def target_mix_for(goal: Goal, years: Decimal, market: MarketData) -> dict[str, Decimal]:
    """目标资产类别比例。应急目标全部货币类；其余按风险区间+滑行路径。"""
    if goal.type == "emergency" or years == 0:
        return {"money": D(1)}
    band = market.risk_band_mix[goal.risk_band]
    stock_cap = equity_fraction_cap(years, market.glide, band["stock"])
    rest = D(1) - stock_cap
    non_stock_total = band["money"] + band["bond"]
    money = rest * band["money"] / non_stock_total
    bond = rest * band["bond"] / non_stock_total
    return {"money": money, "bond": bond, "stock": stock_cap}


def blended_return(mix: dict[str, Decimal], returns: dict[str, Decimal]) -> Decimal:
    out = ZERO
    for klass, weight in mix.items():
        out += weight * returns.get(klass, ZERO)
    return out


def goal_balances(plan: Plan, market: MarketData) -> dict[str, dict[str, Decimal]]:
    """goal -> {cash, money, bond, stock, cash_pending}（多目标账户均摊）。"""
    rows: dict[str, dict[str, Decimal]] = {g.id: {
        "cash": ZERO, "money": ZERO, "bond": ZERO,
        "stock": ZERO, "cash_pending": ZERO} for g in plan.goals}
    for account in plan.accounts:
        share = D(1) / D(len(account.goal_ids)) if account.goal_ids else ZERO
        for gid in account.goal_ids:
            r = rows[gid]
            r["cash"] += account.cash * share
            for lot in account.lots:
                klass = market.instrument_class(lot.instrument_id)
                r[klass] += lot.units * market.prices[lot.instrument_id] * share
            for u in account.unsettled_cash:
                r["cash_pending"] += u.amount * share
    return rows


def pending_settled_by(plan: Plan, on: date, goal_id: str) -> Decimal:
    total = ZERO
    for account in plan.accounts_for(goal_id):
        share = D(1) / D(len(account.goal_ids))
        for u in account.unsettled_cash:
            if u.settlement_date <= on:
                total += u.amount * share
    return total


def analyze(plan: Plan, market: MarketData,
            scenario: Scenario | None = None) -> list[GoalAnalysis]:
    """返回按优先级排序的目标分析。scenario 为空时使用参考表基准。"""
    sc = scenario or market.scenarios["baseline"]
    balances = goal_balances(plan, market)
    result: list[GoalAnalysis] = []
    haircut = D(market.glide["bond_readiness_haircut"])

    for goal in sorted(plan.goals, key=lambda g: g.priority):
        bal = balances[goal.id]
        years = years_between(plan.as_of, goal.target_date)
        due = goal.target_date
        mix = target_mix_for(goal, years, market)
        r_blend = blended_return(mix, sc.returns)

        # 目标关联支出（应急目标吸收 365 天内无目标归属的家庭支出）
        expenses_total = ZERO
        expenses_fv = ZERO
        for ex in plan.expenses:
            attributed = ex.goal_id == goal.id or (
                goal.type == "emergency" and ex.goal_id is None
                and 0 <= (ex.due_date - plan.as_of).days <= 365)
            if not attributed:
                continue
            if due is not None and ex.due_date > due:
                continue
            expenses_total += ex.amount
            t = ZERO if due is None else Decimal((due - ex.due_date).days) / YEARS_DAYS
            expenses_fv += ex.amount * (D(1) + r_blend) ** t

        # 待交收现金：到期日前完成交收才确认；应急只认即时已交收
        cutoff = due if due else plan.as_of
        pending = pending_settled_by(plan, cutoff, goal.id)
        if goal.type == "emergency":
            pending = ZERO

        near = years < D(market.glide["near_term_years"])
        eq_fraction = ZERO if goal.type == "emergency" else _equity_recognition(
            years, market.glide)
        bond_value = bal["bond"] * (D(1) - (haircut if near else ZERO))
        ready = (bal["cash"] + pending + bal["money"] + bond_value
                 + bal["stock"] * eq_fraction)

        # 定投（名义额与到期终值）
        contrib_nominal = ZERO
        contrib_fv = ZERO
        for con in plan.contributions:
            if con.goal_id != goal.id:
                continue
            for d in con.scheduled_dates(due or plan.as_of):
                if d < plan.as_of:
                    continue
                amount = con.amount * (D(1) - sc.contribution_haircut)
                contrib_nominal += amount
                t = ZERO if due is None else Decimal((due - d).days) / YEARS_DAYS
                contrib_fv += amount * (D(1) + r_blend) ** t

        # 现有资产增长至到期：
        # - 长期目标：按各资产类别情景收益分别增值；
        # - 近期目标：必须以确定性现金覆盖，债券按流动性折扣、股票按期限
        #   确认比例折算后，统一按短端利率增值；未确认股票不计入到期覆盖，
        #   其风险通过降风险建议与压力情景体现。
        t = years
        if near:
            recognized = (bal["cash"] + pending + bal["money"]
                          + bal["bond"] * (D(1) - haircut)
                          + bal["stock"] * eq_fraction)
            current_fv = recognized * (D(1) + sc.returns["money"]) ** t
        else:
            current_fv = ZERO
            for klass in ("cash", "money", "bond", "stock"):
                current_fv += bal[klass] * (
                    D(1) + sc.returns.get(klass, ZERO)) ** t
            current_fv += pending
        maturity = current_fv + contrib_fv - expenses_fv
        target = goal.target_amount * sc.target_multiplier
        gap = target - maturity
        ratio = maturity / target if target else ZERO

        constraints: list[dict] = []
        stock_now = bal["stock"]
        total_now = sum((bal[k] for k in ("cash", "money", "bond", "stock")), ZERO)
        if total_now > 0 and goal.type != "emergency":
            stock_weight = stock_now / total_now
            cap_now = equity_fraction_cap(years, market.glide,
                                          market.risk_band_mix[goal.risk_band]["stock"])
            if stock_weight > cap_now + market.drift_band:
                locked_stock = _locked_stock_value(plan, market, goal.id)
                constraints.append({
                    "code": "equity_above_glide",
                    "message": (f"权益占比 {_pct(stock_weight)} 高于期限上限 "
                                f"{_pct(cap_now)}（含漂移带 {_pct(market.drift_band)}）"),
                    "stock_weight": _pct(stock_weight),
                    "equity_cap": _pct(cap_now),
                    "locked_stock_value": cents(locked_stock),
                })
        if gap > 0:
            constraints.append({
                "code": "funding_gap",
                "message": f"到期存在资金缺口 {cents(gap)} 元",
                "gap": cents(gap),
            })

        result.append(GoalAnalysis(
            goal_id=goal.id, goal_type=goal.type, name=goal.name,
            priority=goal.priority, target_date=due,
            horizon_years=str(years.quantize(D("0.01"))),
            target_amount=target,
            assets_by_class={k: bal[k] for k in ("cash", "money", "bond", "stock")},
            pending_cash=pending, recognized_ready=ready,
            bond_haircut=bal["bond"] - bond_value if near else ZERO,
            equity_recognized_fraction=str(eq_fraction.quantize(D("0.01"))),
            scheduled_contributions=contrib_nominal,
            contributions_fv=contrib_fv,
            attributed_expenses=expenses_total,
            projected_maturity_value=maturity, gap=gap,
            funded_ratio=str(ratio.quantize(D("0.001"))),
            target_mix=mix, constraints=constraints,
        ))
    return result


def _equity_recognition(years: Decimal, glide: dict) -> Decimal:
    """1 年内不确认股票用于覆盖缺口，1-10 年线性确认。"""
    if years <= 1:
        return ZERO
    if years >= D(glide["long_term_years"]):
        return D(1)
    return (years - 1) / D(glide["long_term_years"] - 1)


def _locked_stock_value(plan: Plan, market: MarketData, goal_id: str) -> Decimal:
    total = ZERO
    for account in plan.accounts_for(goal_id):
        for lot in account.lots:
            if market.instrument_class(lot.instrument_id) != "stock":
                continue
            if lot.locked or (lot.lockup_until and lot.lockup_until > plan.as_of):
                total += lot.units * market.prices[lot.instrument_id]
    return total


def _pct(v: Decimal) -> str:
    return f"{(v * 100).quantize(D('0.1'))}%"


def stress_matrix(plan: Plan, market: MarketData) -> list[dict]:
    """每个情景 × 每个目标的到期达成情况。"""
    out: list[dict] = []
    for sc in market.scenarios.values():
        for row in analyze(plan, market, sc):
            target = row.target_amount
            out.append({
                "scenario_id": sc.id, "scenario_name": sc.name,
                "goal_id": row.goal_id, "goal_name": row.name,
                "target_date": d_str(row.target_date),
                "target": cents(target),
                "projected_at_maturity": cents(row.projected_maturity_value),
                "shortfall": cents(max(ZERO, row.gap)),
                "surplus": cents(max(ZERO, -row.gap)),
                "funded_ratio": row.funded_ratio,
                "ready_by_due": row.gap <= 0,
            })
    return out
