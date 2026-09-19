from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_tmp = tempfile.mkdtemp(prefix="runtime-test-")
os.environ["RUNTIME_DIR"] = _tmp

from app import create_server  # noqa: E402
from service import SERVICE  # noqa: E402
from storage import VersionStore  # noqa: E402

SERVICE.store = VersionStore(Path(_tmp) / "plans")


def request(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        global PORT
        PORT = cls.port
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def test_01_health(self) -> None:
        status, payload = request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_02_reference(self) -> None:
        status, payload = request("GET", "/reference")
        self.assertEqual(status, 200)
        self.assertIn("glide_path", payload)
        self.assertTrue(payload["instruments"])

    def test_03_seed_and_rebalance_flow(self) -> None:
        status, payload = request("POST", "/plans/seed")
        self.assertEqual(status, 201)
        plan_id = payload["plan_id"]

        status, report = request(
            "POST", f"/plans/{plan_id}/rebalance?trade_date=2026-09-18")
        self.assertEqual(status, 201)
        self.assertEqual(report["status"], "ready_to_execute")
        self.assertTrue(report["recommendations"])
        self.assertIn("gap_resolution", report)
        self.assertIn("stress_after", report)
        for r in report["recommendations"]:
            self.assertIn("rationale", r)
            self.assertIn("cash_change", r)

        # 每条建议都能在缺口归集中找到归属目标
        gids = {g["goal_id"] for g in report["gap_resolution"]}
        self.assertTrue(all(r.get("goal_id") in gids
                            for r in report["recommendations"]
                            if r.get("goal_id")))

    def test_04_closed_market_rebalance(self) -> None:
        status, payload = request("POST", "/plans/seed")
        plan_id = payload["plan_id"]
        status, report = request(
            "POST", f"/plans/{plan_id}/rebalance?trade_date=2026-10-01")
        self.assertEqual(status, 201)
        self.assertEqual(report["status"], "planned_market_closed")

    def test_05_what_if_scoped(self) -> None:
        _, payload = request("POST", "/plans/seed")
        plan_id = payload["plan_id"]
        status, res = request(
            "POST", f"/plans/{plan_id}/what-if",
            {"goals": {"g_housing": {"target_date": "2028-09-01"}}})
        self.assertEqual(status, 201)
        self.assertIn("g_housing", res["affected_goal_ids"])
        self.assertNotIn("g_retirement", res["affected_goal_ids"])

    def test_06_confirm_version_immutable(self) -> None:
        _, payload = request("POST", "/plans/seed")
        plan_id = payload["plan_id"]
        _, report = request(
            "POST", f"/plans/{plan_id}/rebalance")
        vid = report["version"]["version_id"]
        status, confirmed = request(
            "POST", f"/plans/{plan_id}/versions/{vid}/confirm",
            {"confirmed_by": "client", "note": "确认"})
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["status"], "confirmed")
        status, versions = request(
            "GET", f"/plans/{plan_id}/versions")
        self.assertEqual(status, 200)
        self.assertTrue(versions["versions"])
        status, detail = request(
            "GET", f"/plans/{plan_id}/versions/{vid}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["status"], "confirmed")
        self.assertEqual(detail["confirmation"]["note"], "确认")

    def test_07_unknown_plan_404(self) -> None:
        status, payload = request("GET", "/plans/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_08_bad_json_400(self) -> None:
        url = f"http://127.0.0.1:{self.port}/plans/x/what-if"
        req = urllib.request.Request(url, data=b"{bad", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
            self.fail("应当返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


PORT = 0

if __name__ == "__main__":
    unittest.main()
