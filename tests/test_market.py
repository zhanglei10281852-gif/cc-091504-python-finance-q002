from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market import MarketData
from models import parse_date


class CalendarTest(unittest.TestCase):
    def setUp(self) -> None:
        self.m = MarketData()

    def test_weekend_closed(self) -> None:
        self.assertFalse(self.m.is_trading_day(parse_date("2026-09-19")))
        self.assertFalse(self.m.is_trading_day(parse_date("2026-09-20")))
        self.assertTrue(self.m.is_trading_day(parse_date("2026-09-21")))

    def test_national_holiday_closed(self) -> None:
        # 2026-10-01 至 10-07 国庆休市
        self.assertFalse(self.m.is_trading_day(parse_date("2026-10-01")))
        self.assertEqual(
            self.m.next_trading_day(parse_date("2026-10-01")),
            parse_date("2026-10-08"))

    def test_settlement_skips_holiday(self) -> None:
        # T+1：09-30 卖出，10-01 休市，交收顺延
        self.assertEqual(
            self.m.settlement_date(parse_date("2026-09-30"), "CN_MM"),
            parse_date("2026-10-08"))

    def test_reference_tables_loaded(self) -> None:
        self.assertEqual(self.m.currency, "CNY")
        self.assertIn("stock", self.m.classes)
        self.assertEqual(self.m.instruments["CN_STOCK"].lot_size, 100)
        self.assertEqual(self.m.fees.cgt_rate("taxable", 10),
                         self.m.fees.cgt["taxable"].short_rate)
        self.assertEqual(self.m.fees.cgt_rate("housing_special", 10), 0)


if __name__ == "__main__":
    unittest.main()
