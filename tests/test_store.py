"""版本化测试：假设调整只影响相关目标，确认版本可追溯。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from goalplan.service import PlanService, apply_patch  # noqa: E402
from goalplan.store import AlreadyConfirmedError, PlanStore  # noqa: E402
from sample_data import family_payload  # noqa: E402

AS_OF = date(2026, 9, 19)


class ServiceVersioningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = PlanService(storage_root=self.tmp.name)
        created = self.service.create_plan(
            family_payload(), author="advisor-wang", as_of=AS_OF
        )
        self.plan_id = created["plan_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_assumption_change_reports_affected_goals_only(self):
        # 顾问把购房目标从 30 万调到 36 万
        result = self.service.update_assumptions(
            self.plan_id,
            {"goals": [{"id": "goal-house", "target_amount": 360000}]},
            author="advisor-wang",
            as_of=AS_OF,
        )
        self.assertEqual(2, result["version"])
        impact = result["impact"]
        self.assertIn("goal-house", impact["affected_goal_ids"])
        # 退休目标未受影响，不应出现在复核列表里
        self.assertNotIn("goal-retire", impact["affected_goal_ids"])
        house = next(a for a in impact["affected_goals"] if a["goal_id"] == "goal-house")
        self.assertEqual("modified", house["change"])
        self.assertGreater(house["delta_gap"], 0)

    def test_old_version_snapshot_untouched(self):
        before = self.service.get_plan(self.plan_id, 1)
        self.service.update_assumptions(
            self.plan_id,
            {"goals": [{"id": "goal-house", "target_date": "2027-06-01"}]},
            as_of=AS_OF,
        )
        after = self.service.get_plan(self.plan_id, 1)
        self.assertEqual(before, after, "历史版本快照不得被修改")
        house = next(g for g in after["result"]["goal_assessments"]
                     if g["goal_id"] == "goal-house")
        self.assertEqual("2027-03-01", house["target_date"])

    def test_confirm_then_traceable(self):
        confirmed = self.service.confirm(self.plan_id, 1, confirmed_by="客户张先生")
        self.assertTrue(confirmed["confirmed"])
        self.assertEqual("客户张先生", confirmed["confirmed_by"])
        # 重复确认报错
        with self.assertRaises(AlreadyConfirmedError):
            self.service.confirm(self.plan_id, 1, confirmed_by="别人")
        # 确认后仍可基于它生成新版本，父版本链完整
        self.service.update_assumptions(
            self.plan_id, {"assumptions": {"emergency_months": 9}}, as_of=AS_OF
        )
        versions = self.service.list_versions(self.plan_id)
        self.assertEqual([1, 2], [v["version"] for v in versions])
        self.assertEqual(1, versions[1]["parent_version"])
        self.assertTrue(versions[0]["confirmed"])
        self.assertFalse(versions[1]["confirmed"])

    def test_persistence_across_reload(self):
        self.service.update_assumptions(
            self.plan_id, {"assumptions": {"emergency_months": 9}}, as_of=AS_OF
        )
        reloaded = PlanStore(self.tmp.name)
        versions = [v.version for v in reloaded._require(self.plan_id)]
        self.assertEqual([1, 2], versions)


class PatchTest(unittest.TestCase):
    def test_upsert_by_id_and_assumption_merge(self):
        snapshot = {
            "goals": [{"id": "g1", "target_amount": 100, "name": "原"}],
            "assumptions": {"emergency_months": 6, "drift_band": 0.05},
        }
        merged = apply_patch(snapshot, {
            "goals": [{"id": "g1", "target_amount": 200},
                      {"id": "g2", "target_amount": 50, "name": "新"}],
            "assumptions": {"emergency_months": 9},
        })
        g1, g2 = merged["goals"]
        self.assertEqual(200, g1["target_amount"])
        self.assertEqual("原", g1["name"], "未提及字段应保留")
        self.assertEqual(50, g2["target_amount"])
        self.assertEqual(9, merged["assumptions"]["emergency_months"])
        self.assertEqual(0.05, merged["assumptions"]["drift_band"])
        self.assertEqual(100, snapshot["goals"][0]["target_amount"], "原快照不被污染")

    def test_unknown_patch_field_rejected(self):
        with self.assertRaises(ValueError):
            apply_patch({}, {"goalz": []})


if __name__ == "__main__":
    unittest.main()
