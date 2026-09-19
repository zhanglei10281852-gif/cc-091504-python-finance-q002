from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market import MarketData, load_seed_plan
from models import D, Plan, parse_date
from rebalance import generate


def seed() -> tuple[Plan, MarketData]:
    return Plan.from_dict(load_seed_plan()), MarketData()


def sells(report: dict) -> list[dict]:
    return [r for r in report["recommendations"] if r["kind"] == "sell"]


class PriorityTest(unittest.TestCase):
    def test_emergency_funded_first(self) -> None:
        plan, m = seed()
        report = generate(plan, m)
        kinds = [(r["kind"], r.get("goal_id")) for r in report["recommendations"]]
        first = next((k for k in kinds if k[0] != "funding_action"), None)
        self.assertIsNotNone(first)
        self.assertEqual(first[1], "g_emergency")
        em = next(g for g in report["gap_resolution"]
                  if g["goal_id"] == "g_emergency")
        self.assertEqual(em["gap_after"], "0.00")

    def test_emergency_uses_settled_cash_only(self) -> None:
        plan, m = seed()
        # 应急账户只保留待交收资金：不得被当作即时储备
        from models import UnsettledCash
        acct = plan.account("acct_checking")
        acct.cash = D("0")
        acct.unsettled_cash.append(
            UnsettledCash(D("80000"), parse_date("2026-09-21")))
        report = generate(plan, m)
        pending_actions = [r for r in report["recommendations"]
                           if r["kind"] == "funding_action"
                           and r.get("action") == "await_settlement"
                           and r["goal_id"] == "g_emergency"]
        self.assertTrue(pending_actions)

    def test_near_goal_not_cannibalized_for_emergency(self) -> None:
        plan, m = seed()
        report = generate(plan, m)
        # 应急捐赠账户不得包含购房专项账户（近期目标）
        sellers = {r["account_id"] for r in sells(report)
                   if r["goal_id"] == "g_emergency"}
        self.assertNotIn("acct_housing", sellers)
        self.assertNotIn("acct_edu", sellers)
        # 个人养老金账户受限，不得作为捐赠方
        self.assertNotIn("acct_retire", sellers)


class ConstraintTest(unittest.TestCase):
    def test_client_locked_lot_never_sold(self) -> None:
        plan, m = seed()
        report = generate(plan, m)
        sold_lot_ids = {r["lot_id"] for r in sells(report)}
        self.assertNotIn("lot-edu-growth-b", sold_lot_ids)  # 客户锁定
        self.assertNotIn("lot-retire-csi-lockup", sold_lot_ids)  # 禁售

    def test_locked_blocks_education_rebalance_recorded(self) -> None:
        plan, m = seed()
        report = generate(plan, m)
        codes = {(c["code"], c["goal_id"]) for c in report["constraints"]}
        self.assertIn(("locked_blocks_rebalance", "g_education"), codes)
        c = next(c for c in report["constraints"]
                 if c["code"] == "locked_blocks_rebalance")
        blocked_ids = {b["lot_id"] for b in c["blocked_by"]}
        self.assertIn("lot-edu-growth-b", blocked_ids)

    def test_lockup_in_future_unsellable(self) -> None:
        plan, m = seed()
        # 即便养老账户可参与，2027-06-30 才解禁的批次在 2026-09 不可卖
        report = generate(plan, m)
        lockup_sells = [r for r in sells(report)
                        if r["lot_id"] == "lot-retire-csi-lockup"]
        self.assertEqual(lockup_sells, [])

    def test_unsettled_cash_not_assumed_early(self) -> None:
        plan, m = seed()
        report = generate(plan, m)
        housing = next(g for g in report["goal_analysis_before"]
                       if g["goal_id"] == "g_housing")
        # 12000 元于 09-21 交收，早于购房到期，应计入到期资金但单列
        self.assertGreater(D(housing["pending_cash"]), 0)

    def test_remaining_housing_gap_is_reported_not_hidden(self) -> None:
        plan, m = seed()
        report = generate(plan, m)
        gap = next(c for c in report["constraints"]
                   if c["code"] == "funding_gap" and c["goal_id"] == "g_housing")
        self.assertIn("required_monthly_contribution", gap)
        self.assertGreater(D(gap["amount"]), 0)


class LotRoundingTest(unittest.TestCase):
    def test_buy_units_are_lot_multiples(self) -> None:
        plan, m = seed()
        report = generate(plan, m)
        for r in report["recommendations"]:
            if r["kind"] == "buy":
                lot = m.instruments[r["instrument_id"]].lot_size
                units = int(r["units"])
                self.assertEqual(units % lot, 0, r["id"])

    def test_sell_units_are_lot_multiples_and_not_more_than_position(self) -> None:
        plan, m = seed()
        report = generate(plan, m)
        holdings = {}
        for a in plan.accounts:
            for lot in a.lots:
                holdings[lot.id] = lot.units
        for r in sells(report):
            inst = m.instruments[r["instrument_id"]]
            units = Decimal(r["units"])
            self.assertEqual(int(units) % inst.lot_size, 0)
            self.assertLessEqual(units, holdings[r["lot_id"]])


class ClosedMarketTest(unittest.TestCase):
    def test_closed_market_only_plans(self) -> None:
        plan, m = seed()
        report = generate(plan, m, parse_date("2026-10-01"))
        self.assertEqual(report["status"], "planned_market_closed")
        self.assertEqual(report["execution_trade_date"], "2026-10-08")
        for r in report["recommendations"]:
            if r["kind"] in ("sell", "buy", "cash_transfer"):
                self.assertEqual(r["status"], "planned_market_closed")
        self.assertTrue(any(c["code"] == "market_closed"
                            for c in report["constraints"]))

    def test_weekend_is_closed(self) -> None:
        plan, m = seed()
        report = generate(plan, m, parse_date("2026-09-19"))
        self.assertFalse(report["market_open"])
        self.assertEqual(report["execution_trade_date"], "2026-09-21")


class FeeTaxTest(unittest.TestCase):
    def test_sell_carries_commission_stamp_and_tax_detail(self) -> None:
        plan, m = seed()
        report = generate(plan, m)
        edu_sell = next(r for r in sells(report)
                        if r["goal_id"] == "g_education")
        self.assertGreater(D(edu_sell["commission"]), 0)
        self.assertGreater(D(edu_sell["stamp_duty"]), 0)
        # education_special 免资本利得税
        self.assertEqual(edu_sell["capital_gains_tax"], "0.00")
        self.assertEqual(edu_sell["tax_detail"]["tax_status"],
                         "education_special")

    def test_taxable_short_term_gain_taxed(self) -> None:
        plan, m = seed()
        # 给应急制造一个大额缺口，迫使通用账户中盈利的股票批次被卖
        plan.goal("g_emergency").target_amount = D("1200000")
        report = generate(plan, m)
        csi_sells = [r for r in sells(report)
                     if r["instrument_id"] == "CN_STOCK"]
        self.assertTrue(csi_sells)
        taxed = [r for r in csi_sells
                 if D(r["capital_gains_tax"]) > 0]
        self.assertTrue(taxed, "应税账户短期/长期盈利卖出应产生资本利得税")

    def test_cash_impact_totals_balance(self) -> None:
        plan, m = seed()
        report = generate(plan, m)
        impact = report["cash_impact"]
        self.assertGreater(D(impact["total_fees"]), 0)
        # 财富守恒：卖出证券按市价转为现金，家庭总财富只减少费用与税
        self.assertEqual(impact["conservation_check"], "0.00")


class StressTest(unittest.TestCase):
    def test_derisking_helps_in_equity_crash(self) -> None:
        plan, m = seed()
        report = generate(plan, m)
        def ratio(matrix: list, sid: str, gid: str) -> Decimal:
            return D(next(s["funded_ratio"] for s in matrix
                          if s["scenario_id"] == sid and s["goal_id"] == gid))
        before = ratio(report["stress_before"], "equity_crash", "g_education")
        after = ratio(report["stress_after"], "equity_crash", "g_education")
        self.assertGreater(after, before)

    def test_all_scenarios_present(self) -> None:
        plan, m = seed()
        report = generate(plan, m)
        scenarios = {s["scenario_id"] for s in report["stress_after"]}
        self.assertEqual(scenarios, set(m.scenarios.keys()))


if __name__ == "__main__":
    unittest.main()
