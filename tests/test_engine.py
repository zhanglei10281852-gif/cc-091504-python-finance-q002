"""引擎行为测试：现金优先、约束红线、漂移带与休市计划。"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from goalplan.engine import RebalancingEngine  # noqa: E402
from goalplan.models import PlanInput  # noqa: E402
from sample_data import family_payload  # noqa: E402

AS_OF_WEEKEND = date(2026, 9, 19)   # 周六
AS_OF_TRADING = date(2026, 9, 21)   # 周一


def base_payload(**overrides):
    payload = {
        "members": [{"id": "m1", "name": "客户"}],
        "accounts": [
            {"id": "a1", "owner_id": "m1", "name": "应税户",
             "tax_treatment": "taxable", "cash_balance": 0},
        ],
        "lots": [],
        "goals": [],
        "recurring_investments": [],
        "temporary_expenses": [],
        "asset_classes": [
            {"code": "MM", "name": "货币基金", "risk_band": "cash",
             "expected_return": 0.015, "volatility": 0.001,
             "min_trade_unit": 1, "fee_code": "cash"},
            {"code": "EQ", "name": "股票基金", "risk_band": "equity",
             "expected_return": 0.08, "volatility": 0.2,
             "min_trade_unit": 100, "fee_code": "fund"},
            {"code": "BD", "name": "债券基金", "risk_band": "fixed_income",
             "expected_return": 0.03, "volatility": 0.04,
             "min_trade_unit": 100, "fee_code": "fund"},
        ],
        "fee_schedules": [
            {"code": "cash"},
            {"code": "fund", "commission_rate": 0.0001,
             "min_commission": 0, "stamp_duty_rate": 0},
        ],
        "assumptions": {
            "monthly_expenses": 10000,
            "emergency_months": 6,
            "near_term_months": 24,
            "drift_band": 0.05,
            "target_allocation": {},
            "default_buy_assets": {},
            "gains_tax_rates": {"taxable": 0.2},
        },
    }
    for key, value in overrides.items():
        if key == "assumptions":
            payload["assumptions"].update(value)
        else:
            payload[key] = value
    return PlanInput.from_dict(payload)


class EmergencyFirstTest(unittest.TestCase):
    def test_emergency_gap_raises_cash_before_anything(self):
        inp = base_payload(
            accounts=[{"id": "a1", "owner_id": "m1", "name": "应税户",
                       "tax_treatment": "taxable", "cash_balance": 20000}],
            lots=[{"id": "l1", "account_id": "a1", "asset_class": "EQ",
                   "quantity": 10000, "unit_price": 10, "cost_basis": 8}],
        )
        plan = RebalancingEngine().generate(inp, AS_OF_TRADING)
        # 应急目标 6 万，现金 2 万 → 缺口 4 万，卖出股票基金补足
        sells = [r for r in plan.recommendations if r.side == "sell"]
        self.assertTrue(sells)
        first = sells[0]
        self.assertEqual("emergency", first.purposes[0]["type"])
        self.assertEqual(40000, first.gross_amount)
        # 税费：佣金 4 元 + 应税利得 (10-8)*4000*0.2 = 1600
        self.assertEqual(4, first.fee)
        self.assertEqual(1600, first.tax)
        # 净额口径下缺口被完全覆盖（零头会多卖一个交易单位）
        self.assertGreaterEqual(plan.cash_projection["emergency_raised"], 40000)

    def test_emergency_prefers_redeeming_money_fund(self):
        inp = base_payload(
            accounts=[{"id": "a1", "owner_id": "m1", "name": "应税户",
                       "tax_treatment": "taxable", "cash_balance": 20000}],
            lots=[
                {"id": "mm", "account_id": "a1", "asset_class": "MM",
                 "quantity": 30000, "unit_price": 1, "cost_basis": 1},
                {"id": "eq", "account_id": "a1", "asset_class": "EQ",
                 "quantity": 10000, "unit_price": 10, "cost_basis": 8},
            ],
        )
        plan = RebalancingEngine().generate(inp, AS_OF_TRADING)
        sells = [r for r in plan.recommendations if r.side == "sell"]
        self.assertEqual(1, len(sells))
        self.assertEqual("mm", sells[0].lot_id)  # 先赎回货币基金而非卖股票
        self.assertEqual(0, sells[0].tax)


class NearTermGoalTest(unittest.TestCase):
    def test_near_term_goal_derisks_volatile_holdings(self):
        inp = base_payload(
            accounts=[{"id": "a1", "owner_id": "m1", "name": "应税户",
                       "tax_treatment": "taxable", "cash_balance": 100000}],
            lots=[{"id": "eq", "account_id": "a1", "asset_class": "EQ",
                   "quantity": 15000, "unit_price": 10, "cost_basis": 10}],
            goals=[{"id": "g1", "name": "购房首付", "goal_type": "housing",
                    "target_amount": 100000, "target_date": "2027-03-01",
                    "priority": 1, "funding_account_ids": ["a1"]}],
        )
        plan = RebalancingEngine().generate(inp, AS_OF_TRADING)
        goal_sells = [r for r in plan.recommendations
                      if any(p.get("goal_id") == "g1" for p in r.purposes)]
        self.assertTrue(goal_sells, "近期目标应触发降险卖出")
        # 现金池（10 万 − 应急 6 万）+ 卖出净额共同覆盖 10 万目标
        self.assertGreaterEqual(plan.cash_projection["reserved_for_goals"]["g1"], 100000)
        assessment = next(g for g in plan.goal_assessments if g.goal_id == "g1")
        self.assertTrue(assessment.near_term)

    def test_priority_order_when_liquidity_scarce(self):
        inp = base_payload(
            accounts=[{"id": "a1", "owner_id": "m1", "name": "应税户",
                       "tax_treatment": "taxable", "cash_balance": 60000}],
            lots=[{"id": "eq", "account_id": "a1", "asset_class": "EQ",
                   "quantity": 5000, "unit_price": 10, "cost_basis": 10}],
            goals=[
                {"id": "g-low", "name": "低优先级目标", "goal_type": "other",
                 "target_amount": 80000, "target_date": "2027-06-01", "priority": 3},
                {"id": "g-high", "name": "高优先级目标", "goal_type": "housing",
                 "target_amount": 80000, "target_date": "2027-03-01", "priority": 1},
            ],
            assumptions={"monthly_expenses": 0},
        )
        plan = RebalancingEngine().generate(inp, AS_OF_TRADING)
        unmet = [c for c in plan.unmet_constraints if c["code"] == "unfunded_gap"]
        self.assertTrue(unmet)
        # 流动性只够一个目标，高优先级（日期更近）先满足
        self.assertEqual("g-low", unmet[0]["goal_id"])


class ConstraintTest(unittest.TestCase):
    def _plan_with_lot(self, lot):
        inp = base_payload(
            accounts=[{"id": "a1", "owner_id": "m1", "name": "应税户",
                       "tax_treatment": "taxable", "cash_balance": 0}],
            lots=[lot],
        )
        return RebalancingEngine().generate(inp, AS_OF_WEEKEND)

    def test_locked_lot_never_sold(self):
        plan = self._plan_with_lot(
            {"id": "lk", "account_id": "a1", "asset_class": "EQ",
             "quantity": 10000, "unit_price": 10, "cost_basis": 8,
             "status": "locked", "locked_until": "2027-06-01"})
        self.assertEqual([], plan.recommendations)
        codes = {c["code"] for c in plan.unmet_constraints}
        self.assertIn("lot_locked", codes)
        self.assertIn("unfunded_gap", codes)

    def test_client_locked_lot_never_sold(self):
        plan = self._plan_with_lot(
            {"id": "cl", "account_id": "a1", "asset_class": "EQ",
             "quantity": 10000, "unit_price": 10, "cost_basis": 8,
             "client_locked": True})
        self.assertEqual([], plan.recommendations)
        codes = {c["code"] for c in plan.unmet_constraints}
        self.assertIn("client_locked", codes)
        self.assertIn("unfunded_gap", codes)

    def test_pending_settlement_not_sellable_until_settled(self):
        lot = {"id": "pd", "account_id": "a1", "asset_class": "EQ",
               "quantity": 10000, "unit_price": 10, "cost_basis": 8,
               "status": "pending_settlement", "settlement_date": "2026-09-22"}
        inp = base_payload(
            accounts=[{"id": "a1", "owner_id": "m1", "name": "应税户",
                       "tax_treatment": "taxable", "cash_balance": 0}],
            lots=[lot],
        )
        engine = RebalancingEngine()
        before = engine.generate(inp, date(2026, 9, 21))
        self.assertEqual([], before.recommendations)
        self.assertIn("pending_settlement", {c["code"] for c in before.unmet_constraints})
        after = engine.generate(inp, date(2026, 9, 23))
        self.assertTrue(after.recommendations, "交收完成后批次应可用于变现")

    def test_min_trade_unit_blocks_odd_lot(self):
        plan = self._plan_with_lot(
            {"id": "odd", "account_id": "a1", "asset_class": "EQ",
             "quantity": 50, "unit_price": 10, "cost_basis": 8})
        self.assertEqual([], plan.recommendations)
        self.assertIn("min_trade_unit", {c["code"] for c in plan.unmet_constraints})

    def test_sell_rounds_down_to_trade_unit(self):
        inp = base_payload(
            accounts=[{"id": "a1", "owner_id": "m1", "name": "应税户",
                       "tax_treatment": "taxable", "cash_balance": 59950}],
            lots=[{"id": "l1", "account_id": "a1", "asset_class": "EQ",
                   "quantity": 10000, "unit_price": 10, "cost_basis": 10}],
        )
        plan = RebalancingEngine().generate(inp, AS_OF_TRADING)
        sells = [r for r in plan.recommendations if r.side == "sell"]
        self.assertEqual(1, len(sells))
        self.assertEqual(0, sells[0].quantity % 100, "卖出数量必须是交易单位整数倍")


class DriftBandTest(unittest.TestCase):
    def _drift_input(self, eq_qty, bd_qty):
        return base_payload(
            accounts=[{"id": "a1", "owner_id": "m1", "name": "应税户",
                       "tax_treatment": "taxable", "cash_balance": 10000}],
            lots=[
                {"id": "eq", "account_id": "a1", "asset_class": "EQ",
                 "quantity": eq_qty, "unit_price": 10, "cost_basis": 10},
                {"id": "bd", "account_id": "a1", "asset_class": "BD",
                 "quantity": bd_qty, "unit_price": 10, "cost_basis": 10},
            ],
            assumptions={
                "monthly_expenses": 1000,   # 应急目标 6000，现金 1 万已覆盖
                "target_allocation": {"cash": 0.10, "fixed_income": 0.45, "equity": 0.45},
                "default_buy_assets": {"fixed_income": "BD", "equity": "EQ"},
            },
        )

    def test_within_band_no_trade(self):
        # 现金 1 万 + 股 4.5 万 + 债 4.5 万，正好贴着目标配置
        plan = RebalancingEngine().generate(self._drift_input(4500, 4500), AS_OF_TRADING)
        drift_recs = [r for r in plan.recommendations
                      if any(p["type"] == "drift" for p in r.purposes)]
        self.assertEqual([], drift_recs, "漂移带内不应产生调仓建议")

    def test_outside_band_rebalances(self):
        # 股票 6 万 / 债券 3 万：股票超配 10 个百分点，越带
        plan = RebalancingEngine().generate(self._drift_input(6000, 3000), AS_OF_TRADING)
        sells = [r for r in plan.recommendations if r.side == "sell"
                 and any(p["type"] == "drift" for p in r.purposes)]
        buys = [r for r in plan.recommendations if r.side == "buy"]
        self.assertTrue(sells, "越带应卖出超配风险带")
        self.assertTrue(buys, "越带应买入低配风险带")
        self.assertEqual("BD", buys[0].asset_class)


class MarketClosedTest(unittest.TestCase):
    def test_weekend_only_plans_no_execution(self):
        inp = base_payload(
            accounts=[{"id": "a1", "owner_id": "m1", "name": "应税户",
                       "tax_treatment": "taxable", "cash_balance": 0}],
            lots=[{"id": "l1", "account_id": "a1", "asset_class": "EQ",
                   "quantity": 10000, "unit_price": 10, "cost_basis": 8}],
        )
        plan = RebalancingEngine().generate(inp, AS_OF_WEEKEND)
        self.assertEqual("planned_only", plan.status)
        self.assertFalse(plan.market_open)
        self.assertEqual(date(2026, 9, 21), plan.planned_trade_date)
        self.assertTrue(all(not r.executable for r in plan.recommendations))
        self.assertIn("不假定成交", plan.cash_projection["note"])

    def test_trading_day_executable(self):
        inp = base_payload(
            accounts=[{"id": "a1", "owner_id": "m1", "name": "应税户",
                       "tax_treatment": "taxable", "cash_balance": 0}],
            lots=[{"id": "l1", "account_id": "a1", "asset_class": "EQ",
                   "quantity": 10000, "unit_price": 10, "cost_basis": 8}],
        )
        plan = RebalancingEngine().generate(inp, AS_OF_TRADING)
        self.assertEqual("executable", plan.status)
        self.assertTrue(all(r.executable for r in plan.recommendations))


class TaxTreatmentTest(unittest.TestCase):
    def test_tax_deferred_account_sells_without_gains_tax(self):
        inp = base_payload(
            accounts=[{"id": "a1", "owner_id": "m1", "name": "养老户",
                       "tax_treatment": "tax_deferred", "cash_balance": 0}],
            lots=[{"id": "l1", "account_id": "a1", "asset_class": "EQ",
                   "quantity": 10000, "unit_price": 10, "cost_basis": 5}],
        )
        plan = RebalancingEngine().generate(inp, AS_OF_TRADING)
        sells = [r for r in plan.recommendations if r.side == "sell"]
        self.assertTrue(sells)
        self.assertEqual(0, sum(r.tax for r in sells), "递延账户当期不计资本利得税")


class FamilyScenarioTest(unittest.TestCase):
    """题述场景：购房提前 18 个月，教育金账户仍持有波动股票基金。"""

    def test_family_plan(self):
        inp = PlanInput.from_dict(family_payload())
        plan = RebalancingEngine().generate(inp, AS_OF_WEEKEND)
        # 休市：只形成计划
        self.assertEqual("planned_only", plan.status)
        # 购房目标是近期目标，触发教育金账户股票基金降险
        house_sells = [r for r in plan.recommendations
                       if any(p.get("goal_id") == "goal-house" for p in r.purposes)]
        self.assertTrue(house_sells)
        sold_lots = {r.lot_id for r in plan.recommendations if r.side == "sell"}
        # 禁售、待交收、客户锁定批次绝不出现在建议里
        self.assertNotIn("lot-eq-locked", sold_lots)
        self.assertNotIn("lot-bond-pending", sold_lots)
        self.assertNotIn("lot-client-lock", sold_lots)
        # 三类约束都有说明
        codes = {c["code"] for c in plan.unmet_constraints}
        self.assertIn("lot_locked", codes)
        self.assertIn("pending_settlement", codes)
        self.assertIn("client_locked", codes)
        # 每笔建议都解释了用途与税费
        for rec in plan.recommendations:
            self.assertTrue(rec.purposes)
            self.assertTrue(rec.rationale)
        # 远期目标也有评估
        edu = next(g for g in plan.goal_assessments if g.goal_id == "goal-edu")
        self.assertFalse(edu.near_term)


if __name__ == "__main__":
    unittest.main()
