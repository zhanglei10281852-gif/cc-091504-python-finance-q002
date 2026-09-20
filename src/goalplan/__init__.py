"""目标组合再平衡后端核心包。

把家庭成员、目标期限、资金优先级、账户税务属性、持仓批次、
定投与临时支出组织在同一套方案中，输出可解释的调仓建议。
"""

from . import calendar, engine, fees, models, service, store, stress  # noqa: F401
