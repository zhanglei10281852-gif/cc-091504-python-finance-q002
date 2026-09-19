"""目标组合再平衡引擎。

调度顺序（资金优先级不可颠倒）：
1. 应急现金（含无目标归属的一年内临时支出）
2. 近期目标：先在账户内降风险（债券/权益转货币），再统计到期缺口
3. 长期目标：仅当偏离目标比例超过漂移带时才调仓

硬约束（只能记录为未满足约束，不能被建议绕过）：
- locked=True 的客户锁定批次
- lockup_until 晚于交易日的禁售批次
- 最低交易单位 / 最低成交金额
- 待交收资金在交收日前不可用
- 市场休市：所有建议状态为 planned，执行日顺延至下一交易日
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from market import MarketData
from models import (D, ZERO, Plan, Goal, Account, Lot, cents, d_str,
                    parse_date)
from projection import (analyze, stress_matrix, target_mix_for,
                        years_between, equity_fraction_cap)

# 资金来源选择顺序（流动性优先）
SOURCE_CLASS_ORDER = ("money", "bond", "stock")


@dataclass
class WLot:
    lot: Lot
    remaining: Decimal

    @property
    def sellable(self) -> bool:
        return not self.lot.locked and (
            self.lot.lockup_until is None
            or self.lot.lockup_until <= self._trade_day)

    _trade_day: date = date(2099, 1, 1)


@dataclass
class WAccount:
    account: Account
    cash: Decimal
    wlots: list[WLot]

    @classmethod
    def from_account(cls, account: Account, trade_day: date) -> "WAccount":
        return cls(account, account.cash,
                   [WLot(lot, lot.units, trade_day) for lot in account.lots])

    def market_value(self, market: MarketData) -> Decimal:
        total = self.cash
        for w in self.wlots:
            total += w.remaining * market.prices[w.lot.instrument_id]
        return total

    def class_value(self, market: MarketData) -> dict[str, Decimal]:
        out = {"cash": self.cash, "money": ZERO, "bond": ZERO, "stock": ZERO}
        for w in self.wlots:
            klass = market.instrument_class(w.lot.instrument_id)
            out[klass] += w.remaining * market.prices[w.lot.instrument_id]
        return out


@dataclass
class Constraint:
    code: str
    goal_id: str | None
    message: str
    amount: Decimal | None = None
    blocked_by: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {"code": self.code, "goal_id": self.goal_id,
               "message": self.message}
        if self.amount is not None:
            out["amount"] = cents(self.amount)
        if self.blocked_by:
            out["blocked_by"] = self.blocked_by
        out.update(self.extra)
        return out


class RebalanceBuilder:
    """累计建议并保证编号、账户现金、批次余量一致。"""

    def __init__(self, plan: Plan, market: MarketData,
                 requested_day: date) -> None:
        self.plan = plan
        self.market = market
        self.requested_day = requested_day
        self.market_open = market.is_trading_day(requested_day)
        self.trade_day = (requested_day if self.market_open
                          else market.next_trading_day(requested_day))
        self.accounts = {a.id: WAccount.from_account(a, self.trade_day)
                         for a in plan.accounts}
        self.recs: list[dict] = []
        self.constraints: list[Constraint] = []
        self._seq = 0

    @property
    def status(self) -> str:
        return ("ready_to_execute" if self.market_open
                else "planned_market_closed")

    def _sid(self, kind: str) -> str:
        self._seq += 1
        return f"r{self._seq:03d}_{kind}"

    # ---- 税费 ----
    def _sell_economics(self, account: Account, lot: Lot,
                        units: Decimal) -> dict:
        price = self.market.prices[lot.instrument_id]
        proceeds = units * price
        fees = self.market.fees
        commission = max(proceeds * fees.commission_bp, fees.min_commission)
        stamp = proceeds * fees.stamp_duty_bp_sell
        holding_days = (self.plan.as_of - lot.acquisition_date).days
        rate = fees.cgt_rate(account.tax_status, holding_days)
        gain = (price - lot.cost_basis) * units
        tax = max(ZERO, gain) * rate
        net = proceeds - commission - stamp - tax
        return {"price": price, "proceeds": proceeds,
                "commission": commission, "stamp_duty": stamp,
                "capital_gains_tax": tax, "net_cash": net,
                "holding_days": holding_days, "tax_rate": rate,
                "gain": gain}

    def _buy_economics(self, instrument_id: str, units: Decimal) -> dict:
        price = self.market.prices[instrument_id]
        gross = units * price
        commission = max(gross * self.market.fees.commission_bp,
                         self.market.fees.min_commission)
        return {"price": price, "gross": gross,
                "commission": commission, "cash_out": gross + commission}

    # ---- 成交单位取整 ----
    def _round_sell_units(self, lot: Lot, target_net: Decimal,
                          account: Account) -> Decimal:
        """按净到账目标向上取整到最低交易单位。"""
        inst = self.market.instruments[lot.instrument_id]
        price = self.market.prices[lot.instrument_id]
        fees = self.market.fees
        rate = fees.cgt_rate(account.tax_status,
                             (self.plan.as_of - lot.acquisition_date).days)
        net_per_unit = (price
                        - price * (fees.commission_bp + fees.stamp_duty_bp_sell)
                        - max(ZERO, price - lot.cost_basis) * rate)
        if net_per_unit <= 0:
            return ZERO
        raw = target_net / net_per_unit
        units = (raw / inst.lot_size).to_integral_value(
            rounding=ROUND_CEILING) * inst.lot_size
        return min(units, lot.units)

    def _round_buy_units(self, instrument_id: str,
                         cash_budget: Decimal) -> tuple[Decimal, Decimal]:
        """返回 (单位数, 预计现金支出)；受最低成交金额限制时返回 0。"""
        inst = self.market.instruments[instrument_id]
        price = self.market.prices[instrument_id]
        # 预留佣金
        budget_net = cash_budget / (D(1) + self.market.fees.commission_bp)
        units = (budget_net / price / inst.lot_size).to_integral_value(
            rounding=ROUND_FLOOR) * inst.lot_size
        if units <= 0:
            return ZERO, ZERO
        econ = self._buy_economics(instrument_id, units)
        if econ["gross"] < inst.min_trade_amount:
            return ZERO, ZERO
        if econ["cash_out"] > cash_budget:   # 取整后超预算，降一个交易单位
            units -= inst.lot_size
            if units <= 0:
                return ZERO, ZERO
            econ = self._buy_economics(instrument_id, units)
        return units, econ["cash_out"]

    # ---- 建议落账 ----
    def add_sell(self, account_id: str, lot: Lot, units: Decimal,
                 goal_id: str, purpose: str, rationale: str,
                 linked: list[str] | None = None) -> dict:
        wa = self.accounts[account_id]
        econ = self._sell_economics(wa.account, lot, units)
        settle = self.market.settlement_date(self.trade_day, lot.instrument_id)
        rid = self._sid("sell")
        rec = {
            "id": rid, "kind": "sell", "status": self.status,
            "account_id": account_id, "goal_id": goal_id,
            "purpose": purpose, "rationale": rationale,
            "instrument_id": lot.instrument_id, "lot_id": lot.id,
            "side": "sell", "units": str(units),
            "trade_date": d_str(self.trade_day),
            "settlement_date": d_str(settle),
            "gross_amount": cents(econ["proceeds"]),
            "fee": cents(econ["commission"] + econ["stamp_duty"]),
            "commission": cents(econ["commission"]),
            "stamp_duty": cents(econ["stamp_duty"]),
            "capital_gains_tax": cents(econ["capital_gains_tax"]),
            "tax_detail": {
                "tax_status": wa.account.tax_status,
                "acquisition_date": d_str(lot.acquisition_date),
                "holding_days": econ["holding_days"],
                "rate": str(econ["tax_rate"]),
                "taxable_gain": cents(econ["gain"]),
            },
            "cash_change": cents(econ["net_cash"]),
            "cash_change_settled_at": d_str(settle),
            "linked_recommendation_ids": linked or [],
        }
        self.recs.append(rec)
        wa.cash += econ["net_cash"]
        for w in wa.wlots:
            if w.lot.id == lot.id:
                w.remaining -= units
        return rec

    def add_buy(self, account_id: str, instrument_id: str, units: Decimal,
                goal_id: str, purpose: str, rationale: str,
                linked: list[str] | None = None) -> dict:
        wa = self.accounts[account_id]
        econ = self._buy_economics(instrument_id, units)
        settle = self.market.settlement_date(self.trade_day, instrument_id)
        rid = self._sid("buy")
        rec = {
            "id": rid, "kind": "buy", "status": self.status,
            "account_id": account_id, "goal_id": goal_id,
            "purpose": purpose, "rationale": rationale,
            "instrument_id": instrument_id, "side": "buy",
            "units": str(units),
            "trade_date": d_str(self.trade_day),
            "settlement_date": d_str(settle),
            "gross_amount": cents(econ["gross"]),
            "fee": cents(econ["commission"]),
            "commission": cents(econ["commission"]),
            "capital_gains_tax": "0.00",
            "cash_change": cents(-econ["cash_out"]),
            "cash_change_settled_at": d_str(settle),
            "linked_recommendation_ids": linked or [],
        }
        self.recs.append(rec)
        wa.cash -= econ["cash_out"]
        new_lot = Lot(id=f"{rid}-lot", instrument_id=instrument_id,
                      units=units, cost_basis=econ["price"],
                      acquisition_date=self.trade_day, locked=False)
        wa.wlots.append(WLot(new_lot, units, self.trade_day))
        return rec

    def add_transfer(self, from_account: str, to_account: str,
                     amount: Decimal, goal_id: str, rationale: str,
                     linked: list[str],
                     available_at: date | None = None) -> dict:
        avail = available_at or self.trade_day
        rid = self._sid("transfer")
        rec = {
            "id": rid, "kind": "cash_transfer", "status": self.status,
            "account_id": from_account, "to_account_id": to_account,
            "goal_id": goal_id, "purpose": "cover_gap",
            "rationale": rationale,
            "amount": cents(amount),
            "fee": "0.00", "capital_gains_tax": "0.00",
            "cash_change": cents(-amount),
            "cash_change_settled_at": d_str(avail),
            "linked_recommendation_ids": linked,
        }
        self.recs.append(rec)
        self.accounts[from_account].cash -= amount
        self.accounts[to_account].cash += amount
        return rec

    def add_action(self, goal_id: str, action: str, rationale: str,
                   extra: dict) -> dict:
        rid = self._sid("action")

        def _norm(v: object) -> object:
            if isinstance(v, Decimal):
                return cents(v)
            if isinstance(v, date):
                return d_str(v)
            return v

        rec = {"id": rid, "kind": "funding_action", "status": "advisory",
               "goal_id": goal_id, "action": action,
               "rationale": rationale,
               "fee": "0.00", "capital_gains_tax": "0.00",
               "cash_change": "0.00",
               **{k: _norm(v) for k, v in extra.items()}}
        self.recs.append(rec)
        return rec

    # ---- 批次查询 ----
    def free_lots(self, account_ids: list[str], klass: str | None = None
                  ) -> list[tuple[str, Lot, Decimal]]:
        out = []
        for aid in account_ids:
            for w in self.accounts[aid].wlots:
                if w.remaining <= 0 or not w.sellable:
                    continue
                if klass and self.market.instrument_class(
                        w.lot.instrument_id) != klass:
                    continue
                out.append((aid, w.lot, w.remaining))
        return out

    def blocked_lots(self, account_ids: list[str]) -> list[dict]:
        out = []
        for aid in account_ids:
            for w in self.accounts[aid].wlots:
                if w.remaining <= 0:
                    continue
                reasons = []
                if w.lot.locked:
                    reasons.append("client_locked")
                if w.lot.lockup_until and w.lot.lockup_until > self.trade_day:
                    reasons.append("lockup")
                if not reasons:
                    continue
                value = w.remaining * self.market.prices[w.lot.instrument_id]
                out.append({"lot_id": w.lot.id, "account_id": aid,
                            "instrument_id": w.lot.instrument_id,
                            "value": cents(value), "reasons": reasons,
                            "locked_by_client": w.lot.locked,
                            "lockup_until": d_str(w.lot.lockup_until),
                            "note": w.lot.note})
        return out

    def raise_cash(self, target_net: Decimal, donor_account_ids: list[str],
                   goal_id: str, purpose: str, reason_prefix: str
                   ) -> Decimal:
        """按 money→bond→stock、最小增值顺序卖出，返回实际净筹资额。"""
        raised = ZERO
        for klass in SOURCE_CLASS_ORDER:
            if raised >= target_net:
                break
            candidates = self.free_lots(donor_account_ids, klass)
            # 税务效率：单位增值最小的批次先卖
            candidates.sort(key=lambda x: (
                self.market.prices[x[1].instrument_id] - x[1].cost_basis))
            for aid, lot, remaining in candidates:
                need = target_net - raised
                if need <= 0:
                    break
                units = self._round_sell_units(lot, need,
                                               self.accounts[aid].account)
                units = min(units, remaining)
                if units <= 0:
                    continue
                value = units * self.market.prices[lot.instrument_id]
                if value < self.market.instruments[
                        lot.instrument_id].min_trade_amount:
                    self.constraints.append(Constraint(
                        "below_min_trade", goal_id,
                        (f"批次 {lot.id} 拟卖出 {cents(value)} 元，"
                         f"低于最低成交金额，已跳过"),
                        amount=value))
                    continue
                rec = self.add_sell(
                    aid, lot, units, goal_id, purpose,
                    f"{reason_prefix}：卖出 {lot.id} 筹集到期可用现金")
                raised += D(rec["cash_change"])
        return raised


def _unattributed_expenses_within(plan: Plan, days: int) -> Decimal:
    return sum((e.amount for e in plan.expenses
                if e.goal_id is None
                and 0 <= (e.due_date - plan.as_of).days <= days), ZERO)


def _required_monthly(rate_annual: Decimal, months: int,
                      target: Decimal) -> Decimal:
    """期末定投的等额月投入（年化利率按年复利换算月利率）。"""
    if months <= 0:
        return target
    rm = (D(1) + rate_annual) ** (D(1) / D(12)) - D(1)
    if rm == 0:
        return target / months
    factor = ((D(1) + rm) ** months - D(1)) / rm
    return target / factor


def generate(plan: Plan, market: MarketData,
             trade_date: date | None = None) -> dict:
    requested = trade_date or plan.as_of
    b = RebalanceBuilder(plan, market, requested)

    # ========== 阶段 1：应急现金 ==========
    emergency = next((g for g in plan.goals if g.type == "emergency"), None)
    if emergency:
        em_accounts = plan.accounts_for(emergency.id)
        em_ids = {a.id for a in em_accounts}
        need = emergency.target_amount + _unattributed_expenses_within(plan, 365)
        have = sum((b.accounts[a.id].cash for a in em_accounts), ZERO)
        pending = sum((u.amount for a in em_accounts
                       for u in a.unsettled_cash
                       if u.settlement_date > plan.as_of), ZERO)
        if pending:
            b.add_action(
                emergency.id, "await_settlement",
                f"应急账户有 {cents(pending)} 元待交收现金，交收前不计入即时储备",
                {"amount": pending})
        deficit = need - have
        if deficit > 0:
            near_ids = {g.id for g in plan.goals
                        if g.type != "emergency" and g.target_date
                        and years_between(plan.as_of, g.target_date)
                        < D(market.glide["near_term_years"])}
            donor_accounts = [
                a for a in plan.accounts
                if a.id not in em_ids and not a.restricted_withdrawal
                and not (set(a.goal_ids) & near_ids)]
            before_recs = len(b.recs)
            raised = b.raise_cash(
                deficit, [a.id for a in donor_accounts], emergency.id,
                "cover_gap",
                f"应急储备+一年内无归属支出共需 {cents(need)} 元，"
                f"已交收现金 {cents(have)} 元，缺口 {cents(deficit)} 元")
            sell_ids = [r["id"] for r in b.recs[before_recs:]
                        if r["kind"] == "sell"]
            if raised > 0:
                per_seller: dict[str, Decimal] = {}
                for r in b.recs[before_recs:]:
                    if r["kind"] == "sell":
                        per_seller[r["account_id"]] = per_seller.get(
                            r["account_id"], ZERO) + D(r["cash_change"])
                transfer_amount = min(raised, deficit)
                remaining = transfer_amount
                for sid, net in per_seller.items():
                    part = min(net, remaining)
                    if part <= 0:
                        continue
                    latest_settle = max(
                        market.settlement_date(b.trade_day, r["instrument_id"])
                        for r in b.recs
                        if r["kind"] == "sell" and r["account_id"] == sid)
                    tr = b.add_transfer(
                        sid, em_accounts[0].id, part, emergency.id,
                        f"将变现净额 {cents(part)} 元划入应急账户（交收后可用）",
                        sell_ids, available_at=latest_settle)
                    sell_ids.append(tr["id"])
                    remaining -= part
            if raised < deficit:
                b.constraints.append(Constraint(
                    "funding_gap", emergency.id,
                    (f"应急储备仍有 {cents(deficit - raised)} 元缺口："
                     "非近期、非受限账户的可动用批次已用尽，"
                     "锁定/禁售批次不得绕过，近期目标资金不被挤占"),
                    amount=deficit - raised,
                    blocked_by=b.blocked_lots([a.id for a in donor_accounts])))

    # ========== 阶段 2：近期目标（购房）==========
    ordered = sorted(plan.goals, key=lambda g: g.priority)
    for goal in ordered:
        if goal.type in ("emergency", "retirement"):
            continue
        years = years_between(plan.as_of, goal.target_date)
        if years >= D(market.glide["near_term_years"]):
            continue  # 长期目标在阶段 3 处理
        _process_near_goal(b, plan, market, goal, years)

    # ========== 阶段 3：长期目标：漂移带内调仓 ==========
    for goal in ordered:
        if goal.type == "emergency":
            continue
        years = years_between(plan.as_of, goal.target_date)
        if years < D(market.glide["near_term_years"]):
            continue
        _process_long_goal(b, plan, market, goal, years)

    # ========== 休市总约束 ==========
    if not b.market_open:
        b.constraints.append(Constraint(
            "market_closed", None,
            (f"请求交易日 {d_str(requested)} 休市；全部交易建议仅为计划，"
             f"假定成交日顺延至 {d_str(b.trade_day)}，成交价格需在当日重估"),
            extra={"requested_trade_date": d_str(requested),
                   "execution_trade_date": d_str(b.trade_day)}))

    post_plan = _materialize(plan, b)
    before = analyze(plan, market)
    after = analyze(post_plan, market)

    resolution = _gap_resolution(before, after, b.recs)
    stress_before = stress_matrix(plan, market)
    stress_after = stress_matrix(post_plan, market)

    return {
        "plan_id": plan.id,
        "as_of": d_str(plan.as_of),
        "requested_trade_date": d_str(requested),
        "market_open": b.market_open,
        "execution_trade_date": d_str(b.trade_day),
        "status": b.status,
        "scheduling_policy": [
            "先应急现金，再近期目标，后长期配置；近期目标资金不被长期目标挤占",
            "卖出净得需在交收日后方可使用；休市仅形成计划，不假定成交",
            "客户锁定与禁售批次不参与任何卖出建议",
            "长期目标偏离未超过漂移带时不交易",
        ],
        "recommendations": b.recs,
        "gap_resolution": resolution,
        "constraints": [c.to_dict() for c in b.constraints],
        "cash_impact": _cash_impact(b),
        "goal_analysis_before": [x.to_dict() for x in before],
        "goal_analysis_after": [x.to_dict() for x in after],
        "stress_before": stress_before,
        "stress_after": stress_after,
    }


def _gap_resolution(before: list, after: list, recs: list) -> list[dict]:
    """按目标说明每笔建议解决了多少缺口、税费现金代价与残余缺口。"""
    before_map = {x.goal_id: x for x in before}
    rec_ids: dict[str, list[str]] = {}
    for r in recs:
        gid = r.get("goal_id")
        if gid:
            rec_ids.setdefault(gid, []).append(r["id"])
    out = []
    for row in after:
        b0 = before_map[row.goal_id]
        resolved = b0.gap - row.gap
        fees = sum((D(r.get("fee", "0")) for r in recs
                    if r.get("goal_id") == row.goal_id), ZERO)
        tax = sum((D(r.get("capital_gains_tax", "0")) for r in recs
                   if r.get("goal_id") == row.goal_id), ZERO)
        out.append({
            "goal_id": row.goal_id, "goal_name": row.name,
            "gap_before": cents(max(ZERO, b0.gap)),
            "gap_after": cents(max(ZERO, row.gap)),
            "gap_reduced": cents(max(ZERO, resolved)),
            "residual_gap": cents(max(ZERO, row.gap)),
            "funded_ratio_before": b0.funded_ratio,
            "funded_ratio_after": row.funded_ratio,
            "fees_attributable": cents(fees),
            "tax_attributable": cents(tax),
            "recommendation_ids": rec_ids.get(row.goal_id, []),
        })
    return out


def _process_near_goal(b: RebalanceBuilder, plan: Plan, market: MarketData,
                       goal: Goal, years: Decimal) -> None:
    accounts = plan.accounts_for(goal.id)
    ids = [a.id for a in accounts]
    mix = target_mix_for(goal, years, market)

    # 2a. 账户内降风险：权益超过滑行上限（含漂移带）则卖出转货币
    cap = mix["stock"]
    for aid in ids:
        wa = b.accounts[aid]
        cv = wa.class_value(market)
        total = sum(cv.values())
        if total == 0:
            continue
        if cv["stock"] / total > cap + market.drift_band:
            before = len(b.recs)
            target_stock_value = total * cap
            sell_value = cv["stock"] - target_stock_value
            _sell_class_within(b, aid, "stock", sell_value, goal,
                               "de-risk_near_goal",
                               f"近期目标权益超限，降至滑行路径上限 {_pct(cap)}")
            new_cash = sum((D(r["cash_change"]) for r in b.recs[before:]), ZERO)
            if new_cash >= market.min_rebalance_amount:
                _buy_within(b, aid,
                            market.default_instrument_by_class["money"],
                            new_cash, goal, "de-risk_near_goal",
                            "降风险所得转入货币类，锁定到期可用性")

    # 2b. 债券按需提前转货币：只变现覆盖到期确定性缺口所需的部分，
    #     剩余债券继续持有获取票息；迭代至多 3 轮（每轮重算缺口）。
    haircut = D(market.glide["bond_readiness_haircut"])
    for _ in range(3):
        snap = next(x for x in analyze(_materialize(plan, b), market)
                    if x.goal_id == goal.id)
        if snap.gap <= 0:
            break
        progressed = False
        remaining_gap = snap.gap
        for aid in ids:
            if remaining_gap <= 0:
                break
            bond_lots = b.free_lots([aid], "bond")
            if not bond_lots:
                continue
            price = market.prices[bond_lots[0][1].instrument_id]
            position_value = bond_lots[0][2] * price
            # 每变现 1 元债券，确认价值从 (1-haircut) 升为 1（净费用），
            # 故所需变现毛额 ≈ 缺口 / haircut，并用持仓上限截断
            target_net = min(remaining_gap / haircut, position_value)
            units = b._round_sell_units(
                bond_lots[0][1], target_net, b.accounts[aid].account)
            units = min(units, bond_lots[0][2])
            if units <= 0:
                continue
            rec = b.add_sell(
                aid, bond_lots[0][1], units, goal.id, "ready_cash",
                (f"距到期 {years} 年，债券市值计提 {_pct(haircut)} 流动性折扣；"
                 "为覆盖到期确定性缺口，按需赎回转货币类，未动用债券继续持有"))
            new_cash = D(rec["cash_change"])
            if new_cash >= market.min_rebalance_amount:
                bought = _buy_within(
                    b, aid, market.default_instrument_by_class["money"],
                    new_cash, goal, "ready_cash",
                    "赎回资金转入货币类，T+1 交收后可用")
                if bought:
                    progressed = True
            remaining_gap = next(
                x.gap for x in analyze(_materialize(plan, b), market)
                if x.goal_id == goal.id)
        if not progressed:
            break

    # 2c. 待交收资金提示
    pending = sum((u.amount for a in accounts for u in a.unsettled_cash), ZERO)
    if pending:
        b.add_action(
            goal.id, "await_settlement",
            f"{goal.name}账户有 {cents(pending)} 元待交收资金，将按交收日确认",
            {"amount": pending,
             "settles": sorted({d_str(u.settlement_date) for a in accounts
                                for u in a.unsettled_cash})})

    # 2d. 到期缺口：统计定投后仍不足的部分（不挪用其他目标资产）
    after_row = next(x for x in analyze(_materialize(plan, b), market)
                     if x.goal_id == goal.id)
    if after_row.gap > 0:
        months = max(1, (goal.target_date.year - plan.as_of.year) * 12
                     + goal.target_date.month - plan.as_of.month)
        pmt = _required_monthly(market.scenarios["baseline"].returns["money"],
                                months, after_row.gap)
        current_ctb = sum((c.amount for c in plan.contributions
                           if c.goal_id == goal.id), ZERO)
        blocked = b.blocked_lots([a.id for a in plan.accounts])
        b.constraints.append(Constraint(
            "funding_gap", goal.id,
            (f"{goal.name}在完成账户内降风险与既定定投后，到期仍缺 "
             f"{cents(after_row.gap)} 元；系统未挪用其他目标资产，"
             "也未绕过客户锁定/禁售批次"),
            amount=after_row.gap,
            blocked_by=blocked,
            extra={"required_monthly_contribution": cents(pmt),
                   "months_to_due": months,
                   "current_monthly_contribution": cents(current_ctb)}))
        b.add_action(
            goal.id, "increase_contribution_or_adjust_goal",
            (f"建议将月定投由 {cents(current_ctb)} 元提高至约 {cents(pmt)} 元，"
             "或重新评估目标金额/到期日；锁定与禁售资产不在自动建议范围内"),
            {"required_monthly_contribution": cents(pmt),
             "remaining_gap": cents(after_row.gap)})


def _process_long_goal(b: RebalanceBuilder, plan: Plan, market: MarketData,
                       goal: Goal, years: Decimal) -> None:
    mix = target_mix_for(goal, years, market)
    accounts = plan.accounts_for(goal.id)
    account_ids = [a.id for a in accounts]
    total = sum((b.accounts[a.id].market_value(market) for a in accounts), ZERO)
    if total == 0:
        return
    target_value = {k: total * w for k, w in mix.items()}
    current = {"cash": ZERO, "money": ZERO, "bond": ZERO, "stock": ZERO}
    for a in accounts:
        for k, v in b.accounts[a.id].class_value(market).items():
            current[k] += v

    stock_dev = current["stock"] / total - mix["stock"]
    if abs(stock_dev) <= market.drift_band:
        return  # 漂移带内，不交易

    linked: list[str] = []
    if stock_dev > 0:
        excess = current["stock"] - target_value["stock"]
        bond_deficit = max(ZERO, target_value["bond"] - current["bond"])
        free = b.free_lots(account_ids, "stock")
        free.sort(key=lambda x: (
            market.prices[x[1].instrument_id] - x[1].cost_basis))
        for aid, lot, remaining in free:
            if excess <= 0:
                break
            units = min(remaining, b._round_sell_units(
                lot, excess, b.accounts[aid].account))
            value = units * market.prices[lot.instrument_id]
            if units <= 0 or value < market.min_rebalance_amount:
                continue
            rec = b.add_sell(
                aid, lot, units, goal.id, "drift_rebalance",
                (f"权益占比超出漂移带（上限 {_pct(mix['stock'] + market.drift_band)}），"
                 "税务效率优先卖出后再平衡至目标区间"))
            linked.append(rec["id"])
            excess -= D(rec["gross_amount"])
            cash = D(rec["cash_change"])
            bond_budget = min(cash, bond_deficit)
            if bond_budget >= market.min_rebalance_amount:
                br = _buy_within(b, aid,
                                 market.default_instrument_by_class["bond"],
                                 bond_budget, goal, "drift_rebalance",
                                 "再平衡：超配权益转配债券", linked)
                if br:
                    linked.append(br["id"])
                    spent = -D(br["cash_change"])
                    cash -= spent
                    bond_deficit -= spent
            if cash >= market.min_rebalance_amount:
                mr = _buy_within(b, aid,
                                 market.default_instrument_by_class["money"],
                                 cash, goal, "drift_rebalance",
                                 "再平衡：余款转货币类", linked)
                if mr:
                    linked.append(mr["id"])
        # 锁定/禁售导致无法回到漂移带内
        stock_now = all_now = ZERO
        for a in accounts:
            cv = b.accounts[a.id].class_value(market)
            stock_now += cv["stock"]
            all_now += sum(cv.values())
        if all_now and stock_now / all_now > mix["stock"] + market.drift_band:
            b.constraints.append(Constraint(
                "locked_blocks_rebalance", goal.id,
                (f"{goal.name}权益仍高于漂移带，但可卖批次已用尽；"
                 "客户锁定/禁售批次不能被建议绕过"),
                blocked_by=b.blocked_lots(account_ids)))
    else:
        # 权益不足：仅使用目标账户内已交收闲置现金回补，不制造杠杆、
        # 不动用近期目标资金
        shortfall = target_value["stock"] - current["stock"]
        for a in accounts:
            budget = min(b.accounts[a.id].cash, shortfall)
            if budget < market.min_rebalance_amount:
                continue
            br = _buy_within(b, a.id,
                             market.default_instrument_by_class["stock"],
                             budget, goal, "drift_rebalance",
                             "权益低于漂移带下沿，使用闲置已交收现金回补")
            if br:
                shortfall -= -D(br["cash_change"])
        if shortfall > 0:
            b.constraints.append(Constraint(
                "no_cash_to_rebalance", goal.id,
                (f"{goal.name}权益低于目标，回补仍差 {cents(shortfall)} 元，"
                 "但账户无足够已交收现金；不建议借款或动用近期目标资金"),
                amount=shortfall))


def _sell_class_within(b: RebalanceBuilder, account_id: str, klass: str,
                       value_target: Decimal, goal: Goal, purpose: str,
                       rationale: str) -> None:
    need = value_target
    for aid, lot, remaining in b.free_lots([account_id], klass):
        if need <= 0:
            break
        price = b.market.prices[lot.instrument_id]
        units = min(remaining, b._round_sell_units(lot, need,
                                                   b.accounts[aid].account))
        if units <= 0:
            continue
        rec = b.add_sell(aid, lot, units, goal.id, purpose, rationale)
        need -= D(rec["cash_change"])


def _buy_within(b: RebalanceBuilder, account_id: str, instrument_id: str,
                cash_budget: Decimal, goal: Goal, purpose: str,
                rationale: str, linked: list[str] | None = None
                ) -> dict | None:
    wa = b.accounts[account_id]
    budget = min(cash_budget, wa.cash)
    if budget < b.market.min_rebalance_amount:
        return None
    units, cash_out = b._round_buy_units(instrument_id, budget)
    if units <= 0:
        b.constraints.append(Constraint(
            "below_min_trade", goal.id,
            (f"买入 {instrument_id} 受最低交易单位/金额限制，"
             f"预算 {cents(budget)} 元无法形成有效委托"),
            amount=budget))
        return None
    return b.add_buy(account_id, instrument_id, units, goal.id, purpose,
                     rationale, linked)


def _materialize(plan: Plan, b: RebalanceBuilder) -> Plan:
    """根据累计建议构造调仓后方案快照（用于测算与压力测试）。"""
    new_accounts = []
    for aid, wa in b.accounts.items():
        lots = [Lot(id=w.lot.id, instrument_id=w.lot.instrument_id,
                    units=w.remaining, cost_basis=w.lot.cost_basis,
                    acquisition_date=w.lot.acquisition_date,
                    locked=w.lot.locked, lockup_until=w.lot.lockup_until,
                    note=w.lot.note)
                for w in wa.wlots if w.remaining > 0]
        new_accounts.append(dataclasses.replace(
            wa.account, cash=wa.cash, lots=lots))
    return dataclasses.replace(plan, accounts=new_accounts)


def _cash_impact(b: RebalanceBuilder) -> dict:
    per_account: dict[str, dict] = {}
    totals = {"fees": ZERO, "tax": ZERO}
    for r in b.recs:
        if r["kind"] not in ("sell", "buy", "cash_transfer"):
            continue
        if r["kind"] != "cash_transfer":
            totals["fees"] += D(r.get("fee", "0"))
            totals["tax"] += D(r.get("capital_gains_tax", "0"))
            row = per_account.setdefault(r["account_id"], {
                "fees": ZERO, "tax": ZERO, "net_cash_change": ZERO})
            row["fees"] += D(r.get("fee", "0"))
            row["tax"] += D(r.get("capital_gains_tax", "0"))
    # 现金变动以最终账户余额对比初始余额（含转入/转出与交收前净额）
    for aid in {a.id for a in b.plan.accounts} | set(b.accounts):
        row = per_account.setdefault(aid, {
            "fees": ZERO, "tax": ZERO, "net_cash_change": ZERO})
        start = next(a.cash for a in b.plan.accounts if a.id == aid)
        row["net_cash_change"] = b.accounts[aid].cash - start
    # 家庭总财富（现金+证券市值）变动应恰好等于 -(总费用+总税)
    prices = b.market.prices
    wealth_before = sum(
        (a.market_value(prices) for a in b.plan.accounts), ZERO)
    wealth_after = sum(
        (wa.market_value(b.market) for wa in b.accounts.values()), ZERO)
    # 待交收资金未计入 market_value（调仓不改变它），两边同口径纳入
    pending = sum((u.amount for a in b.plan.accounts
                   for u in a.unsettled_cash), ZERO)
    wealth_before += pending
    wealth_after += pending
    wealth_change = wealth_after - wealth_before
    return {
        "currency": "CNY",
        "total_fees": cents(totals["fees"]),
        "total_tax": cents(totals["tax"]),
        "wealth_change": cents(wealth_change),
        "conservation_check": cents(
            wealth_change + totals["fees"] + totals["tax"]),
        "by_account": {aid: {k: cents(v) for k, v in row.items()}
                       for aid, row in per_account.items()},
    }


def _pct(v: Decimal) -> str:
    return f"{(v * 100).quantize(D('0.1'))}%"
