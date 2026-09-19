from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market import MarketData, load_seed_plan
from models import D, Plan
from scenarios import PatchError, what_if
from storage import VersionStore
from rebalance import generate


class WhatIfTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = Plan.from_dict(load_seed_plan())
        self.market = MarketData()

    def test_only_affected_goals_returned(self) -> None:
        res = what_if(self.plan, self.market,
                      {"goals": {"g_education": {"target_amount": "900000"}}})
        self.assertEqual(res["affected_goal_ids"], ["g_education"])
        self.assertTrue(all(d["goal_id"] != "g_retirement"
                            for d in res["goal_diffs"]))

    def test_housing_date_change_recomputes_gap(self) -> None:
        res = what_if(self.plan, self.market,
                      {"goals": {"g_housing": {"target_date": "2028-09-01"}}})
        diff = next(d for d in res["goal_diffs"]
                    if d["goal_id"] == "g_housing")
        # 期限拉长 18 个月后缺口应缩小
        self.assertLess(D(diff["gap_after"]), D(diff["gap_before"]))

    def test_emergency_months_updates_target(self) -> None:
        res = what_if(self.plan, self.market,
                      {"assumptions": {"emergency_months": 9}})
        after = next(g for g in res["after_report"]["goal_analysis_after"]
                     if g["goal_id"] == "g_emergency")
        # 9 * 33000 = 297000（另加 30000 无归属年度支出）
        self.assertEqual(after["target_amount"], "297000.00")

    def test_unknown_field_rejected(self) -> None:
        with self.assertRaises(PatchError):
            what_if(self.plan, self.market,
                    {"goals": {"g_housing": {"locked": True}}})

    def test_risk_band_change_touches_non_emergency_goals(self) -> None:
        res = what_if(self.plan, self.market,
                      {"assumptions": {"default_risk_band": "growth"}})
        self.assertNotIn("g_emergency", res["affected_goal_ids"])
        self.assertIn("g_retirement", res["affected_goal_ids"])


class VersionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = VersionStore(Path(self.tmp.name) / "plans")
        self.plan = Plan.from_dict(load_seed_plan())
        self.market = MarketData()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_version_chain_and_immutability(self) -> None:
        report1 = generate(self.plan, self.market)
        v1 = self.store.add_version(self.plan.id, self.plan.to_dict(), report1,
                                    label="base")
        report2 = generate(self.plan, self.market)
        v2 = self.store.add_version(self.plan.id, self.plan.to_dict(), report2,
                                    label="tweak")
        self.assertEqual(v2["parent_version_id"], v1["version_id"])
        confirmed = self.store.confirm_version(
            self.plan.id, v1["version_id"], note="客户已确认")
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertIsNotNone(confirmed["confirmation"])
        # 再次确认不改变已确认内容
        again = self.store.confirm_version(self.plan.id, v1["version_id"])
        self.assertEqual(again["confirmation"], confirmed["confirmation"])
        chain = self.store.ancestry(self.plan.id, v2["version_id"])
        self.assertEqual(chain, [v2["version_id"], v1["version_id"]])

    def test_versions_listed(self) -> None:
        report = generate(self.plan, self.market)
        self.store.add_version(self.plan.id, self.plan.to_dict(), report)
        versions = self.store.list_versions(self.plan.id)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["status"], "draft")


if __name__ == "__main__":
    unittest.main()
