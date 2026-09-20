"""API 端到端测试：建方案 → 调假设 → 确认 → 追溯。"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from app import create_server  # noqa: E402
from goalplan.service import PlanService  # noqa: E402
from sample_data import family_payload  # noqa: E402


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        service = PlanService(storage_root=cls.tmp.name)
        cls.server = create_server("127.0.0.1", 0, service=service)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def _request(self, method: str, path: str, body: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_advisory_flow(self):
        # 健康检查
        status, health = self._request("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", health["status"])

        # 休市日（周六）创建方案：只形成计划
        payload = family_payload()
        payload["as_of"] = "2026-09-19"
        status, plan = self._request("POST", "/api/plans", payload)
        self.assertEqual(201, status)
        plan_id = plan["plan_id"]
        self.assertEqual("planned_only", plan["result"]["status"])
        self.assertTrue(plan["result"]["recommendations"])
        # 每笔建议都解释了缺口归属、税费与现金变化
        rec = plan["result"]["recommendations"][0]
        for field in ("purposes", "rationale", "estimated_fee",
                      "estimated_tax", "net_cash_delta", "cash_after"):
            self.assertIn(field, rec)
        # 未满足约束与压力情景随方案返回
        self.assertIn("unmet_constraints", plan["result"])
        self.assertTrue(plan["stress"]["scenarios"])

        # 顾问调整假设：购房提前到 2027-01 → 受影响目标报告
        status, updated = self._request("POST", f"/api/plans/{plan_id}/assumptions", {
            "patch": {"goals": [{"id": "goal-house", "target_date": "2027-01-15"}]},
            "author": "advisor-wang",
            "as_of": "2026-09-19",
            "change_summary": "购房计划再提前一个半月",
        })
        self.assertEqual(201, status)
        self.assertEqual(2, updated["version"])
        self.assertIn("goal-house", updated["impact"]["affected_goal_ids"])

        # 客户确认 v2，版本可追溯
        status, confirmed = self._request(
            "POST", f"/api/plans/{plan_id}/confirm",
            {"version": 2, "confirmed_by": "客户张先生"},
        )
        self.assertEqual(200, status)
        self.assertTrue(confirmed["confirmed"])

        status, versions = self._request("GET", f"/api/plans/{plan_id}/versions")
        self.assertEqual(200, status)
        self.assertEqual(2, len(versions["versions"]))
        self.assertFalse(versions["versions"][0]["confirmed"])
        self.assertTrue(versions["versions"][1]["confirmed"])

        # 历史版本仍可读取
        status, v1 = self._request("GET", f"/api/plans/{plan_id}/versions/1")
        self.assertEqual(200, status)
        house = next(g for g in v1["result"]["goal_assessments"]
                     if g["goal_id"] == "goal-house")
        self.assertEqual("2027-03-01", house["target_date"])

        # 压力情景接口
        status, stress = self._request("GET", f"/api/plans/{plan_id}/stress")
        self.assertEqual(200, status)
        names = {s["name"] for s in stress["scenarios"]}
        self.assertIn("equity_crash", names)
        for scenario in stress["scenarios"]:
            for goal in scenario["goals"]:
                self.assertIn("achievement_ratio", goal)
                self.assertIn("met", goal)

    def test_market_status(self):
        status, body = self._request("GET", "/api/market/status?date=2026-10-03")
        self.assertEqual(200, status)
        self.assertFalse(body["is_trading_day"])  # 国庆假期
        self.assertEqual("2026-10-09", body["next_trading_day"])
        status, body = self._request("GET", "/api/market/status?date=2026-09-21")
        self.assertTrue(body["is_trading_day"])

    def test_error_handling(self):
        status, body = self._request("GET", "/api/plans/no-such-plan")
        self.assertEqual(404, status)
        status, body = self._request("POST", "/api/plans", {"goals": "not-a-list"})
        self.assertEqual(400, status)
        status, body = self._request("POST", "/api/plans", {"bad": True})
        self.assertEqual(400, status)
        self.assertIn("accounts", body["error"]["message"])
        status, _ = self._request("GET", "/no/such/route")
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
