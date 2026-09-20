"""交易日历：周末与示例节假日，T+N 交收。

市场休市时引擎只形成计划（planned_only），不假定成交；
建议的计划交易日顺延至下一交易日。
"""

from __future__ import annotations

from datetime import date, timedelta

# 示例节假日（公开信息整理，可按年更新；仅用于演示与测试）
SAMPLE_HOLIDAYS: tuple[str, ...] = (
    # 2026 年国庆节 / 中秋节
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06",
    "2026-10-07", "2026-10-08",
    # 2027 年元旦、春节
    "2027-01-01",
    "2027-02-05", "2027-02-08", "2027-02-09", "2027-02-10", "2027-02-11",
)


class TradingCalendar:
    def __init__(self, holidays: tuple[str, ...] = SAMPLE_HOLIDAYS, settlement_lag: int = 1):
        self.holidays = {date.fromisoformat(h) for h in holidays}
        self.settlement_lag = settlement_lag

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def next_trading_day(self, day: date) -> date:
        candidate = day + timedelta(days=1)
        while not self.is_trading_day(candidate):
            candidate += timedelta(days=1)
        return candidate

    def settlement_date(self, trade_day: date) -> date:
        """T+N 交收：从交易日起数 N 个交易日。"""
        remaining = self.settlement_lag
        candidate = trade_day
        while remaining > 0:
            candidate = self.next_trading_day(candidate)
            remaining -= 1
        return candidate

    def status(self, day: date) -> dict:
        trading = self.is_trading_day(day)
        nxt = day if trading else self.next_trading_day(day)
        return {
            "date": day.isoformat(),
            "is_trading_day": trading,
            "next_trading_day": nxt.isoformat(),
            "settlement_date": self.settlement_date(nxt).isoformat(),
            "settlement_lag_days": self.settlement_lag,
        }
