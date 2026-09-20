"""费用与税费样例。

仅为可配置的示例参数，不构成任何实际费率承诺：
- 佣金按比例收取并设最低收费；
- 印花税仅卖出方向收取；
- 应税账户按卖出盈利计提资本利得税，递延/免税账户当期不计。
"""

from __future__ import annotations

from decimal import Decimal

from .models import D, FeeSchedule, TaxTreatment, money

DEFAULT_FEE_SCHEDULES: dict[str, FeeSchedule] = {
    # 股票样例：佣金万 2.5、最低 5 元、卖出印花税万 5
    "equity": FeeSchedule(
        code="equity",
        commission_rate=D("0.00025"),
        min_commission=D("5"),
        stamp_duty_rate=D("0.0005"),
    ),
    # 场内基金样例：佣金万 1、无最低收费、免印花税
    "fund": FeeSchedule(
        code="fund",
        commission_rate=D("0.0001"),
        min_commission=D("0"),
        stamp_duty_rate=D("0"),
    ),
    # 现金管理类样例：申赎零费用
    "cash": FeeSchedule(code="cash"),
}

DEFAULT_GAINS_TAX_RATES: dict[str, float] = {
    TaxTreatment.TAXABLE.value: 0.20,
    TaxTreatment.TAX_DEFERRED.value: 0.0,
    TaxTreatment.TAX_FREE.value: 0.0,
}


def capital_gains_tax(
    treatment: TaxTreatment,
    unit_price: Decimal,
    cost_basis: Decimal,
    quantity: Decimal,
    rates: dict[str, float],
) -> Decimal:
    """应税账户按盈利部分计提；亏损批次当期税额为 0（不做抵免假设）。"""
    rate = D(rates.get(treatment.value, 0.0))
    if rate <= 0:
        return Decimal("0.00")
    gain_per_unit = unit_price - cost_basis
    if gain_per_unit <= 0:
        return Decimal("0.00")
    return money(gain_per_unit * quantity * rate)
