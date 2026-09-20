"""再平衡引擎。

处理顺序（与需求一致）：
1. 先保证应急现金（月支出 × 覆盖月数）；
2. 再处理近期目标（默认 24 个月内到期）：把目标对应资产降险至安全档，
   按目标优先级依次弥补现金缺口；
3. 最后在漂移带内处理长期配置（超出漂移带才调仓）。

约束红线：禁售批次、待交收批次、客户锁定资产不得被建议绕过；
卖出数量向下取整到最低交易单位；市场休市时只形成计划、不假定成交。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_CEILING
from typing import Any

from .calendar import TradingCalendar
from .fees import capital_gains_tax
from .models import (
    D,
    Account,
    FeeSchedule,
    Goal,
    HoldingLot,
    LotStatus,
    PlanInput,
    RiskBand,
    Side,
    TaxTreatment,
    money,
)

# 税务效率排序：先动应税账户，保留递延/免税账户的复利空间
_TAX_RANK = {
    TaxTreatment.TAXABLE: 0,
    TaxTreatment.TAX_DEFERRED: 1,
    TaxTreatment.TAX_FREE: 2,
}


@dataclass
class TradeRecommendation:
    rec_id: str
    account_id: str
    asset_class: str
    side: Side
    quantity: Decimal
    unit_price: Decimal
    gross_amount: Decimal
    fee: Decimal
    tax: Decimal
    net_cash_delta: Decimal     # 卖出为正（现金流入），买入为负
    cash_after: Decimal         # 该笔之后的家庭现金余额
    purposes: list[dict[str, Any]]  # 每笔建议解决了哪个缺口
    rationale: str
    planned_trade_date: date
    executable: bool
    lot_id: str | None = None   # 卖出对应的持仓批次（买入为 None）

    def to_dict(self) -> dict[str, Any]:
        return {
            "rec_id": self.rec_id,
            "account_id": self.account_id,
            "asset_class": self.asset_class,
            "side": self.side.value,
            "quantity": float(self.quantity),
            "unit_price": float(self.unit_price),
            "gross_amount": float(self.gross_amount),
            "estimated_fee": float(self.fee),
            "estimated_tax": float(self.tax),
            "net_cash_delta": float(self.net_cash_delta),
            "cash_after": float(self.cash_after),
            "purposes": self.purposes,
            "rationale": self.rationale,
            "planned_trade_date": self.planned_trade_date.isoformat(),
            "executable": self.executable,
            "lot_id": self.lot_id,
        }


@dataclass
class GoalAssessment:
    goal_id: str
    name: str
    goal_type: str
    priority: int
    target_amount: Decimal
    target_date: date
    horizon_months: float
    near_term: bool
    earmarked_value: Decimal
    projected_value: Decimal
    gap: Decimal
    funded_ratio: float
    status: str  # on_track / at_risk / shortfall

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "name": self.name,
            "goal_type": self.goal_type,
            "priority": self.priority,
            "target_amount": float(self.target_amount),
            "target_date": self.target_date.isoformat(),
            "horizon_months": round(self.horizon_months, 1),
            "near_term": self.near_term,
            "earmarked_value": float(self.earmarked_value),
            "projected_value": float(self.projected_value),
            "gap": float(self.gap),
            "funded_ratio": round(self.funded_ratio, 4),
            "status": self.status,
        }


@dataclass
class RebalancePlan:
    as_of: date
    market_open: bool
    status: str                 # executable / planned_only
    planned_trade_date: date
    recommendations: list[TradeRecommendation]
    goal_assessments: list[GoalAssessment]
    unmet_constraints: list[dict[str, Any]]
    cash_projection: dict[str, Any]
    allocation_before: dict[str, float]
    allocation_after: dict[str, float]
    totals: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "market_open": self.market_open,
            "status": self.status,
            "planned_trade_date": self.planned_trade_date.isoformat(),
            "recommendations": [r.to_dict() for r in self.recommendations],
            "goal_assessments": [g.to_dict() for g in self.goal_assessments],
            "unmet_constraints": self.unmet_constraints,
            "cash_projection": self.cash_projection,
            "allocation_before": self.allocation_before,
            "allocation_after": self.allocation_after,
            "totals": self.totals,
        }


@dataclass
class _Need:
    """一笔待弥补的现金缺口（应急金或某个近期目标）。"""

    kind: str            # emergency / goal
    goal: Goal | None
    amount: Decimal
    remaining: Decimal
    safe_bands: set[RiskBand] = field(default_factory=set)

    @property
    def label(self) -> str:
        return "应急现金储备" if self.kind == "emergency" else f"目标「{self.goal.name}」"


@dataclass
class _Context:
    """引擎工作上下文：累积建议、未满足约束与现金流水。"""

    inp: PlanInput
    as_of: date
    trade_date: date
    executable: bool
    accounts: dict[str, Account]
    cash: Decimal = Decimal("0")
    recommendations: list[TradeRecommendation] = field(default_factory=list)
    unmet: list[dict[str, Any]] = field(default_factory=list)
    # lot_id -> 已建议卖出数量（同一批次可被多笔缺口消耗）
    sold: dict[str, Decimal] = field(default_factory=dict)
    # asset_code -> 已建议买入金额（用于调仓后配置测算）
    bought: dict[str, Decimal] = field(default_factory=dict)

    def remaining_qty(self, lot: HoldingLot) -> Decimal:
        return lot.quantity - self.sold.get(lot.id, Decimal("0"))

    def note(self, code: str, message: str, severity: str = "warning", **extra: Any) -> None:
        entry = {"code": code, "severity": severity, "message": message}
        entry.update(extra)
        self.unmet.append(entry)


class RebalancingEngine:
    def __init__(self, calendar: TradingCalendar | None = None):
        self.calendar = calendar or TradingCalendar()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def generate(
        self,
        inp: PlanInput,
        as_of: date,
        market_open: bool | None = None,
    ) -> RebalancePlan:
        if market_open is None:
            market_open = self.calendar.is_trading_day(as_of)
        trade_date = as_of if market_open else self.calendar.next_trading_day(as_of)
        ctx = _Context(
            inp=inp,
            as_of=as_of,
            trade_date=trade_date,
            executable=market_open,
            accounts=inp.account_map(),
            cash=sum((a.cash_balance for a in inp.accounts), Decimal("0")),
        )
        start_cash = ctx.cash

        self._note_blocked_assets(ctx)

        # 1. 应急现金
        a = inp.assumptions
        emergency_target = money(a.monthly_expenses * a.emergency_months)
        emergency_need = self._emergency_need(ctx)
        if emergency_need:
            self._raise_cash(ctx, emergency_need)
        # 2. 近期目标（按目标日期、再按优先级排序）；应急目标之外的现金先用于目标
        pool = max(Decimal("0"), ctx.cash - emergency_target)
        goal_needs, cash_allocs, assessments = self._near_term_needs(ctx, pool)
        goal_reserved = sum(cash_allocs.values(), Decimal("0"))
        for need in goal_needs:
            self._raise_cash(ctx, need)
            goal_reserved += need.amount - need.remaining

        # 3. 长期配置：漂移带内不动，越带才调。
        # 可投资现金 = 当前现金 − 应急目标全额 − 为目标预留 − 近期临时支出
        allocation_before = self._band_allocation(ctx, before=True)
        upcoming_exp = sum(
            (e.amount for e in inp.temporary_expenses
             if e.goal_id is None
             and 0 <= (e.date - as_of).days <= a.near_term_months * 31),
            Decimal("0"),
        )
        reserved = emergency_target + goal_reserved + upcoming_exp
        free_cash = max(Decimal("0"), ctx.cash - reserved)
        self._rebalance_drift(ctx, free_cash)
        allocation_after = self._band_allocation(ctx, before=False)

        # 远期目标评估（不参与现金筹集，只报告）
        assessments += self._long_term_assessments(ctx, {a.goal_id for a in assessments})

        plan_status = "executable" if market_open else "planned_only"
        return RebalancePlan(
            as_of=as_of,
            market_open=market_open,
            status=plan_status,
            planned_trade_date=trade_date,
            recommendations=ctx.recommendations,
            goal_assessments=sorted(assessments, key=lambda a: (a.target_date, a.priority)),
            unmet_constraints=ctx.unmet,
            cash_projection={
                "start_cash": float(start_cash),
                "end_cash": float(ctx.cash),
                "emergency_target": float(emergency_target),
                "emergency_raised": float(
                    emergency_need.amount - emergency_need.remaining if emergency_need else 0
                ),
                "reserved_for_goals": {
                    **{gid: float(amount) for gid, amount in cash_allocs.items()},
                    **{
                        n.goal.id: float(
                            cash_allocs.get(n.goal.id, Decimal("0")) + n.amount - n.remaining
                        )
                        for n in goal_needs
                    },
                },
                "upcoming_temp_expenses": float(upcoming_exp),
                "free_cash_after_plan": float(
                    max(Decimal("0"), ctx.cash - emergency_target - goal_reserved - upcoming_exp)
                ),
                "note": "市场休市，仅形成计划不假定成交" if not market_open else "建议待客户确认后执行",
            },
            allocation_before=allocation_before,
            allocation_after=allocation_after,
            totals={
                "portfolio_value": float(self._total_value(ctx)),
                "total_estimated_fees": float(sum(r.fee for r in ctx.recommendations)),
                "total_estimated_taxes": float(sum(r.tax for r in ctx.recommendations)),
            },
        )

    # ------------------------------------------------------------------
    # 约束提示：禁售 / 待交收 / 客户锁定
    # ------------------------------------------------------------------

    def _note_blocked_assets(self, ctx: _Context) -> None:
        for lot in ctx.inp.lots:
            asset = ctx.inp.asset_classes.get(lot.asset_class)
            name = asset.name if asset else lot.asset_class
            if lot.client_locked:
                ctx.note(
                    "client_locked",
                    f"批次 {lot.id}（{name}）已被客户锁定，未纳入任何调仓建议",
                    severity="info",
                    lot_id=lot.id,
                )
            elif lot.status == LotStatus.LOCKED and not lot.sellable(ctx.as_of):
                ctx.note(
                    "lot_locked",
                    f"禁售批次 {lot.id}（{name}）至 {lot.locked_until} 前不可卖出，相关缺口需另寻流动性",
                    severity="info",
                    lot_id=lot.id,
                    locked_until=lot.locked_until.isoformat() if lot.locked_until else None,
                )
            elif lot.status == LotStatus.PENDING_SETTLEMENT and not lot.sellable(ctx.as_of):
                ctx.note(
                    "pending_settlement",
                    f"批次 {lot.id}（{name}）待交收，{lot.settlement_date} 起方可动用",
                    severity="info",
                    lot_id=lot.id,
                    settlement_date=lot.settlement_date.isoformat() if lot.settlement_date else None,
                )
        for acc in ctx.inp.accounts:
            for pending in acc.pending_cash:
                if pending.settle_date > ctx.as_of:
                    ctx.note(
                        "pending_cash",
                        f"账户 {acc.name} 有待交收资金 {pending.amount}，{pending.settle_date} 到账",
                        severity="info",
                        account_id=acc.id,
                        amount=float(pending.amount),
                        settle_date=pending.settle_date.isoformat(),
                    )

    # ------------------------------------------------------------------
    # 第一步：应急现金
    # ------------------------------------------------------------------

    def _emergency_need(self, ctx: _Context) -> _Need | None:
        a = ctx.inp.assumptions
        target = money(a.monthly_expenses * a.emergency_months)
        liquid = ctx.cash + sum(
            (lot.market_value for lot in self._sellable_lots(ctx)
             if self._band_of(ctx, lot) == RiskBand.CASH),
            Decimal("0"),
        )
        gap = target - liquid
        if gap <= 0:
            return None
        return _Need(kind="emergency", goal=None, amount=gap, remaining=gap)

    # ------------------------------------------------------------------
    # 第二步：近期目标
    # ------------------------------------------------------------------

    def _near_term_needs(
        self, ctx: _Context, pool: Decimal
    ) -> tuple[list[_Need], dict[str, Decimal], list[GoalAssessment]]:
        """近期目标：先分配应急之外的现金池，再把剩余波动资产降险。"""
        a = ctx.inp.assumptions
        needs: list[_Need] = []
        cash_allocs: dict[str, Decimal] = {}
        assessments: list[GoalAssessment] = []
        near_goals = sorted(
            (g for g in ctx.inp.goals if g.horizon_months(ctx.as_of) <= a.near_term_months),
            key=lambda g: (g.target_date, g.priority),
        )
        for goal in near_goals:
            assessment = self._assess_goal(ctx, goal, near_term=True)
            assessments.append(assessment)
            horizon = goal.horizon_months(ctx.as_of)
            safe_bands = {RiskBand.CASH} if horizon <= 12 else {RiskBand.CASH, RiskBand.FIXED_INCOME}
            earmarked = self._earmarked_lots(ctx, goal)
            safe_value = sum(
                (lot.market_value for lot in earmarked if self._band_of(ctx, lot) in safe_bands),
                Decimal("0"),
            )
            recurring_fv = self._recurring_fv(ctx, goal)
            temp_out = self._temp_expenses(ctx, goal)
            raw_need = goal.target_amount - safe_value - recurring_fv + temp_out
            if raw_need <= 0:
                continue
            cash_alloc = min(raw_need, pool)
            pool -= cash_alloc
            if cash_alloc > 0:
                cash_allocs[goal.id] = cash_alloc
            remaining = raw_need - cash_alloc
            if remaining <= 0:
                continue
            need = _Need(kind="goal", goal=goal, amount=remaining, remaining=remaining)
            need.safe_bands = safe_bands  # 筹集时不得赎回已算入覆盖的安全档资产
            needs.append(need)
        return needs, cash_allocs, assessments

    # ------------------------------------------------------------------
    # 现金筹集：按税务效率顺序卖出，归属到具体缺口
    # ------------------------------------------------------------------

    def _raise_cash(self, ctx: _Context, need: _Need) -> None:
        candidates = self._sell_order(ctx, self._sellable_lots(ctx))
        if need.kind == "emergency":
            # 应急缺口优先赎回现金类资产
            candidates.sort(key=lambda lot: 0 if self._band_of(ctx, lot) == RiskBand.CASH else 1)
        else:
            # 目标缺口的覆盖已计入安全档资产，卖出它们只是原地换手
            candidates = [
                lot for lot in candidates
                if self._band_of(ctx, lot) not in need.safe_bands
            ]
            if need.goal and need.goal.funding_account_ids:
                # 目标缺口优先动用其归属账户
                funding = set(need.goal.funding_account_ids)
                candidates.sort(key=lambda lot: 0 if lot.account_id in funding else 1)
        # 净额口径下可能需要多轮（税费使单笔净入小于毛额），直到缺口闭合或无可卖
        while need.remaining > 0:
            progress = False
            for lot in candidates:
                if need.remaining <= 0:
                    break
                asset = ctx.inp.asset_classes[lot.asset_class]
                remaining_qty = ctx.remaining_qty(lot)
                if remaining_qty <= 0:
                    continue
                unit = asset.min_trade_unit
                sellable_qty = (remaining_qty // unit) * unit
                if sellable_qty <= 0:
                    ctx.note(
                        "min_trade_unit",
                        f"批次 {lot.id} 剩余数量低于最低交易单位 {unit}，无法为{need.label}变现",
                        lot_id=lot.id,
                        goal_id=need.goal.id if need.goal else None,
                    )
                    continue
                # 需要多少卖多少（向上取整到交易单位），不超过该批次可卖数量。
                # 注意 Decimal 的 // 是向零截断，向上取整须用 ROUND_CEILING。
                units_needed = (need.remaining / (lot.unit_price * unit)).to_integral_value(
                    rounding=ROUND_CEILING
                )
                qty = min(sellable_qty, units_needed * unit)
                if qty <= 0:
                    continue
                before = need.remaining
                rec = self._record_sell(
                    ctx, lot, qty,
                    purposes=[{
                        "type": need.kind,
                        "goal_id": need.goal.id if need.goal else None,
                        "goal_name": need.goal.name if need.goal else "应急现金储备",
                        "covered_amount": float(min(before, money(qty * lot.unit_price))),
                    }],
                    rationale=f"为{need.label}筹集现金 {float(min(before, money(qty * lot.unit_price))):,.2f}",
                )
                need.remaining -= rec.net_cash_delta
                progress = True
            if not progress:
                break
        if need.remaining > 0:
            self._explain_shortfall(ctx, need)

    def _explain_shortfall(self, ctx: _Context, need: _Need) -> None:
        """缺口未能闭合时，说明是哪类约束挡住了。"""
        blocked_value = Decimal("0")
        reasons: list[str] = []
        for lot in ctx.inp.lots:
            if lot.sellable(ctx.as_of) or ctx.remaining_qty(lot) <= 0:
                continue
            blocked_value += lot.market_value
            if lot.client_locked:
                reasons.append(f"客户锁定批次 {lot.id}")
            elif lot.status == LotStatus.LOCKED:
                reasons.append(f"禁售批次 {lot.id}（{lot.locked_until} 解锁）")
            elif lot.status == LotStatus.PENDING_SETTLEMENT:
                reasons.append(f"待交收批次 {lot.id}（{lot.settlement_date} 到账）")
        detail = "；".join(reasons) if reasons else "可用流动性已耗尽"
        ctx.note(
            "unfunded_gap",
            f"{need.label}仍有缺口 {need.remaining} 无法覆盖：{detail}",
            severity="error",
            goal_id=need.goal.id if need.goal else None,
            remaining_gap=float(need.remaining),
            blocked_value=float(blocked_value),
        )

    # ------------------------------------------------------------------
    # 第三步：长期配置漂移
    # ------------------------------------------------------------------

    def _rebalance_drift(self, ctx: _Context, free_cash: Decimal) -> None:
        a = ctx.inp.assumptions
        if not a.target_allocation:
            return
        total = self._total_value(ctx)
        if total <= 0:
            return
        current = self._band_values(ctx)
        sells: list[tuple[RiskBand, Decimal]] = []
        buys: list[tuple[RiskBand, Decimal]] = []
        for band_name, target_w in a.target_allocation.items():
            band = RiskBand(band_name)
            if band == RiskBand.CASH:
                continue  # 现金超配通常源于为目标预留，交给买入侧处理
            cur_w = float(current.get(band, Decimal("0"))) / float(total)
            drift = cur_w - target_w
            if abs(drift) <= a.drift_band:
                continue  # 漂移带内不动作
            amount = money(abs(drift) * float(total))
            if drift > 0:
                sells.append((band, amount))
            else:
                buys.append((band, amount))

        raised = Decimal("0")
        for band, amount in sells:
            raised += self._sell_band_for_drift(ctx, band, amount)
        buy_budget = free_cash + raised
        for band, amount in buys:
            budgeted = min(amount, buy_budget)
            spent = self._buy_band(ctx, band, budgeted) if budgeted > 0 else Decimal("0")
            buy_budget -= spent
            if spent < amount:
                ctx.note(
                    "insufficient_cash",
                    f"风险带 {band.value} 低配 {amount}，可用资金仅补足 {spent}",
                    band=band.value,
                    shortfall=float(amount - spent),
                )

    def _sell_band_for_drift(self, ctx: _Context, band: RiskBand, amount: Decimal) -> Decimal:
        raised = Decimal("0")
        remaining = amount
        candidates = [
            lot for lot in self._sell_order(ctx, self._sellable_lots(ctx))
            if self._band_of(ctx, lot) == band and ctx.remaining_qty(lot) > 0
        ]
        for lot in candidates:
            if remaining <= 0:
                break
            asset = ctx.inp.asset_classes[lot.asset_class]
            unit = asset.min_trade_unit
            sellable_qty = (ctx.remaining_qty(lot) // unit) * unit
            want_qty = (remaining // lot.unit_price // unit) * unit
            qty = min(sellable_qty, want_qty)
            if qty <= 0:
                continue
            gross = money(qty * lot.unit_price)
            self._record_sell(
                ctx, lot, qty,
                purposes=[{
                    "type": "drift",
                    "goal_id": None,
                    "goal_name": None,
                    "band": band.value,
                    "covered_amount": float(gross),
                }],
                rationale=f"风险带 {band.value} 超出漂移带，减持 {float(gross):,.2f} 回归目标配置",
            )
            remaining -= gross
            raised += gross
        if remaining > 0:
            ctx.note(
                "drift_unresolved",
                f"风险带 {band.value} 仍有 {remaining} 超配无法调出（受禁售/锁定/交易单位限制）",
                band=band.value,
                remaining=float(remaining),
            )
        return raised

    def _buy_band(self, ctx: _Context, band: RiskBand, amount: Decimal) -> Decimal:
        asset_code = ctx.inp.assumptions.default_buy_assets.get(band.value)
        if not asset_code or asset_code not in ctx.inp.asset_classes:
            ctx.note(
                "no_default_asset",
                f"风险带 {band.value} 未配置默认买入资产，低配 {amount} 未处理",
                band=band.value,
                amount=float(amount),
            )
            return Decimal("0")
        asset = ctx.inp.asset_classes[asset_code]
        account = self._buy_account(ctx)
        price = self._reference_price(ctx, asset_code)
        unit = asset.min_trade_unit
        qty = (amount // price // unit) * unit
        if qty <= 0:
            ctx.note(
                "min_trade_unit",
                f"买入 {asset.name} 的金额 {amount} 不足一个最低交易单位，已跳过",
                asset_class=asset_code,
                amount=float(amount),
            )
            return Decimal("0")
        gross = money(qty * price)
        fee = self._fee_schedule(ctx, asset.fee_code).fee_for(Side.BUY, gross)
        ctx.cash -= gross + fee
        ctx.bought[asset_code] = ctx.bought.get(asset_code, Decimal("0")) + gross
        ctx.recommendations.append(TradeRecommendation(
            rec_id=f"R{len(ctx.recommendations) + 1}",
            account_id=account.id,
            asset_class=asset_code,
            side=Side.BUY,
            quantity=qty,
            unit_price=price,
            gross_amount=gross,
            fee=fee,
            tax=Decimal("0.00"),
            net_cash_delta=-(gross + fee),
            cash_after=ctx.cash,
            purposes=[{
                "type": "drift",
                "goal_id": None,
                "goal_name": None,
                "band": band.value,
                "covered_amount": float(gross),
            }],
            rationale=f"风险带 {band.value} 低于目标配置，买入 {asset.name} 补足",
            planned_trade_date=ctx.trade_date,
            executable=ctx.executable,
        ))
        return gross

    # ------------------------------------------------------------------
    # 目标评估与测算
    # ------------------------------------------------------------------

    def _assess_goal(self, ctx: _Context, goal: Goal, near_term: bool) -> GoalAssessment:
        earmarked = self._earmarked_lots(ctx, goal)
        value = sum((lot.market_value for lot in earmarked), Decimal("0"))
        projected = self._project_goal(ctx, goal)
        gap = goal.target_amount - projected
        ratio = float(projected / goal.target_amount) if goal.target_amount > 0 else 1.0
        if gap <= 0:
            status = "on_track"
        elif ratio >= 0.8:
            status = "at_risk"
        else:
            status = "shortfall"
        return GoalAssessment(
            goal_id=goal.id,
            name=goal.name,
            goal_type=goal.goal_type.value,
            priority=goal.priority,
            target_amount=goal.target_amount,
            target_date=goal.target_date,
            horizon_months=goal.horizon_months(ctx.as_of),
            near_term=near_term,
            earmarked_value=value,
            projected_value=money(projected),
            gap=money(max(gap, Decimal("0"))),
            funded_ratio=ratio,
            status=status,
        )

    def _long_term_assessments(
        self, ctx: _Context, already: set[str]
    ) -> list[GoalAssessment]:
        return [
            self._assess_goal(ctx, g, near_term=False)
            for g in ctx.inp.goals
            if g.id not in already
        ]

    def _project_goal(self, ctx: _Context, goal: Goal) -> Decimal:
        """到期测算： earmarked 持仓按预期收益增长 + 定投终值 − 临时支出。"""
        years = max(0.0, (goal.target_date - ctx.as_of).days / 365.25)
        total = Decimal("0")
        for lot in self._earmarked_lots(ctx, goal):
            asset = ctx.inp.asset_classes.get(lot.asset_class)
            er = asset.expected_return if asset else 0.0
            total += D(float(lot.market_value) * (1 + er) ** years)
        total += self._recurring_fv(ctx, goal)
        total -= self._temp_expenses(ctx, goal)
        return total

    def _recurring_fv(self, ctx: _Context, goal: Goal) -> Decimal:
        total = Decimal("0")
        funding = set(goal.funding_account_ids) or {a.id for a in ctx.inp.accounts}
        for rec in ctx.inp.recurring_investments:
            if rec.goal_id and rec.goal_id != goal.id:
                continue
            if not rec.goal_id and rec.account_id not in funding:
                continue
            start = rec.next_date or ctx.as_of
            if start >= goal.target_date:
                continue
            months = (goal.target_date - start).days / 30.4375
            n = int(months // rec.interval_months)
            if n <= 0:
                continue
            asset = ctx.inp.asset_classes.get(rec.asset_class)
            er = asset.expected_return if asset else 0.0
            i = (1 + er) ** (rec.interval_months / 12) - 1
            if i <= 0:
                fv = float(rec.amount) * n
            else:
                fv = float(rec.amount) * (((1 + i) ** n - 1) / i)
            total += D(fv)
        return total

    def _temp_expenses(self, ctx: _Context, goal: Goal) -> Decimal:
        """目标层面的临时支出：仅计入显式归属该目标的条目。

        未指定目标的临时支出在方案层面从可投资现金中统一扣减，
        避免与目标测算重复计算。
        """
        total = Decimal("0")
        for exp in ctx.inp.temporary_expenses:
            if exp.goal_id != goal.id:
                continue
            if ctx.as_of <= exp.date <= goal.target_date:
                total += exp.amount
        return total

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _record_sell(
        self,
        ctx: _Context,
        lot: HoldingLot,
        qty: Decimal,
        purposes: list[dict[str, Any]],
        rationale: str,
    ) -> TradeRecommendation:
        asset = ctx.inp.asset_classes[lot.asset_class]
        account = ctx.accounts[lot.account_id]
        gross = money(qty * lot.unit_price)
        fee = self._fee_schedule(ctx, asset.fee_code).fee_for(Side.SELL, gross)
        tax = capital_gains_tax(
            account.tax_treatment, lot.unit_price, lot.cost_basis, qty,
            ctx.inp.assumptions.gains_tax_rates,
        )
        net = gross - fee - tax
        ctx.cash += net
        ctx.sold[lot.id] = ctx.sold.get(lot.id, Decimal("0")) + qty
        rec = TradeRecommendation(
            rec_id=f"R{len(ctx.recommendations) + 1}",
            account_id=lot.account_id,
            asset_class=lot.asset_class,
            side=Side.SELL,
            quantity=qty,
            unit_price=lot.unit_price,
            gross_amount=gross,
            fee=fee,
            tax=tax,
            net_cash_delta=net,
            cash_after=ctx.cash,
            purposes=purposes,
            rationale=rationale,
            planned_trade_date=ctx.trade_date,
            executable=ctx.executable,
            lot_id=lot.id,
        )
        ctx.recommendations.append(rec)
        return rec

    def _fee_schedule(self, ctx: _Context, code: str) -> FeeSchedule:
        """费用样例缺失时退化为零费用，而不是让引擎崩溃。"""
        return ctx.inp.fee_schedules.get(code) or FeeSchedule(code=code)

    def _sellable_lots(self, ctx: _Context) -> list[HoldingLot]:
        return [lot for lot in ctx.inp.lots if lot.sellable(ctx.as_of)]

    def _sell_order(self, ctx: _Context, lots: list[HoldingLot]) -> list[HoldingLot]:
        """税务与成本意识排序：应税账户优先、亏损批次优先、费率低者优先。"""

        def key(lot: HoldingLot) -> tuple:
            account = ctx.accounts[lot.account_id]
            asset = ctx.inp.asset_classes[lot.asset_class]
            gain_ratio = float(lot.unit_price - lot.cost_basis) / float(lot.unit_price)
            fee_rate = self._fee_schedule(ctx, asset.fee_code).commission_rate
            return (_TAX_RANK[account.tax_treatment], gain_ratio, fee_rate, lot.id)

        return sorted(lots, key=key)

    def _earmarked_lots(self, ctx: _Context, goal: Goal) -> list[HoldingLot]:
        funding = set(goal.funding_account_ids)
        if not funding:
            return list(ctx.inp.lots)
        return [lot for lot in ctx.inp.lots if lot.account_id in funding]

    def _band_of(self, ctx: _Context, lot: HoldingLot) -> RiskBand:
        asset = ctx.inp.asset_classes.get(lot.asset_class)
        return asset.risk_band if asset else RiskBand.CASH

    def _band_values(self, ctx: _Context) -> dict[RiskBand, Decimal]:
        values: dict[RiskBand, Decimal] = {b: Decimal("0") for b in RiskBand}
        for lot in ctx.inp.lots:
            remaining_value = money(ctx.remaining_qty(lot) * lot.unit_price)
            values[self._band_of(ctx, lot)] += remaining_value
        for code, amount in ctx.bought.items():
            asset = ctx.inp.asset_classes.get(code)
            band = asset.risk_band if asset else RiskBand.CASH
            values[band] += amount
        values[RiskBand.CASH] += ctx.cash
        return values

    def _band_allocation(self, ctx: _Context, before: bool) -> dict[str, float]:
        if before:
            values: dict[RiskBand, Decimal] = {b: Decimal("0") for b in RiskBand}
            for lot in ctx.inp.lots:
                values[self._band_of(ctx, lot)] += lot.market_value
            values[RiskBand.CASH] += sum(
                (a.cash_balance for a in ctx.inp.accounts), Decimal("0")
            )
        else:
            values = self._band_values(ctx)
        total = sum(values.values(), Decimal("0"))
        if total <= 0:
            return {b.value: 0.0 for b in RiskBand}
        return {b.value: round(float(v) / float(total), 4) for b, v in values.items()}

    def _total_value(self, ctx: _Context) -> Decimal:
        lots_value = sum(
            (money(ctx.remaining_qty(lot) * lot.unit_price) for lot in ctx.inp.lots),
            Decimal("0"),
        )
        bought = sum(ctx.bought.values(), Decimal("0"))
        return lots_value + bought + ctx.cash

    def _buy_account(self, ctx: _Context) -> Account:
        """买入落在现金最充裕的应税账户（样例规则，可替换）。"""
        return max(
            ctx.inp.accounts,
            key=lambda a: (a.tax_treatment == TaxTreatment.TAXABLE, a.cash_balance),
        )

    def _reference_price(self, ctx: _Context, asset_code: str) -> Decimal:
        for lot in ctx.inp.lots:
            if lot.asset_class == asset_code:
                return lot.unit_price
        return Decimal("1")
