from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from models import (Plan, Goal, Account, Lot, Contribution, UnsettledCash,
                    parse_date, D)


class ModelTest(unittest.TestCase):
    def test_decimal_from_string_not_float(self) -> None:
        self.assertEqual(D("0.1") + D("0.2"), Decimal("0.3"))

    def test_goal_validation(self) -> None:
        with self.assertRaises(ValueError):
            Goal("g", "housing", "房", None, D("100"), None, 1, "conservative")
        with self.assertRaises(ValueError):
            Goal("g", "unknown", "x", None, D("100"),
                 parse_date("2027-01-01"), 1, "conservative")
        with self.assertRaises(ValueError):
            Goal("g", "housing", "x", None, D("0"),
                 parse_date("2027-01-01"), 1, "conservative")

    def test_emergency_goal_allows_no_date(self) -> None:
        g = Goal("g", "emergency", "应急", None, D("100"), None, 1,
                 "conservative")
        self.assertEqual(g.type, "emergency")

    def test_scheduled_dates_monthly(self) -> None:
        con = Contribution("c", "g", "a", D("8000"), "monthly", 20,
                           parse_date("2026-10-20"),
                           parse_date("2027-02-20"))
        dates = con.scheduled_dates(parse_date("2030-01-01"))
        self.assertEqual([d.isoformat() for d in dates],
                         ["2026-10-20", "2026-11-20", "2026-12-20",
                          "2027-01-20", "2027-02-20"])

    def test_scheduled_dates_respects_upto(self) -> None:
        con = Contribution("c", "g", "a", D("1"), "monthly", 1,
                           parse_date("2026-01-01"), None)
        dates = con.scheduled_dates(parse_date("2026-03-01"))
        self.assertEqual(len(dates), 3)

    def test_settled_cash_by(self) -> None:
        a = Account("a", "账户", "", "taxable", ["g"], D("10"),
                    [UnsettledCash(D("100"), parse_date("2026-09-21"))], [])
        self.assertEqual(a.settled_cash_by(parse_date("2026-09-18")), D("10"))
        self.assertEqual(a.settled_cash_by(parse_date("2026-09-21")), D("110"))

    def test_plan_rejects_dangling_reference(self) -> None:
        payload = {
            "id": "p", "as_of": "2026-09-18",
            "household": {"id": "h", "base_currency": "CNY", "members": []},
            "goals": [{"id": "g1", "type": "housing", "target_amount": "100",
                       "target_date": "2027-03-01", "priority": 1,
                       "risk_band": "conservative"}],
            "accounts": [{"id": "a1", "tax_status": "taxable",
                          "goal_ids": ["missing"]}],
        }
        with self.assertRaises(ValueError):
            Plan.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
