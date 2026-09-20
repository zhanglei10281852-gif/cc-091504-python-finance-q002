"""题述家庭场景样例：购房计划提前 18 个月，教育金账户仍持有波动股票基金。"""

from __future__ import annotations

from typing import Any

AS_OF = "2026-09-19"  # 周六，市场休市


def family_payload() -> dict[str, Any]:
    return {
        "members": [
            {"id": "m-self", "name": "张先生", "relation": "self"},
            {"id": "m-spouse", "name": "李女士", "relation": "spouse"},
            {"id": "m-child", "name": "小张", "relation": "child"},
        ],
        "accounts": [
            {"id": "acc-cash", "owner_id": "m-self", "name": "现金管理户",
             "tax_treatment": "taxable", "cash_balance": 80000},
            {"id": "acc-edu", "owner_id": "m-self", "name": "教育金专户",
             "tax_treatment": "taxable", "cash_balance": 5000},
            {"id": "acc-inv", "owner_id": "m-spouse", "name": "家庭投资户",
             "tax_treatment": "taxable", "cash_balance": 15000,
             "pending_cash": [
                 {"amount": 30000, "settle_date": "2026-09-22",
                  "note": "上周赎回待到账"},
             ]},
            {"id": "acc-retire", "owner_id": "m-self", "name": "个人养老户",
             "tax_treatment": "tax_deferred", "cash_balance": 0},
        ],
        "lots": [
            {"id": "lot-mm", "account_id": "acc-cash", "asset_class": "CASH-MM",
             "quantity": 20000, "unit_price": 1.0, "cost_basis": 1.0},
            # 教育金账户里的波动股票基金（可交易）
            {"id": "lot-eq-edu", "account_id": "acc-edu", "asset_class": "EQ-FUND",
             "quantity": 30000, "unit_price": 2.0, "cost_basis": 1.5},
            # 禁售批次：2027-06-01 前不可卖
            {"id": "lot-eq-locked", "account_id": "acc-edu", "asset_class": "EQ-STOCK",
             "quantity": 5000, "unit_price": 10.0, "cost_basis": 6.0,
             "status": "locked", "locked_until": "2027-06-01"},
            # 待交收批次：2026-09-22 起可动
            {"id": "lot-bond-pending", "account_id": "acc-inv", "asset_class": "BOND-IDX",
             "quantity": 2000, "unit_price": 50.0, "cost_basis": 49.0,
             "status": "pending_settlement", "settlement_date": "2026-09-22"},
            # 客户锁定资产：不得纳入建议
            {"id": "lot-client-lock", "account_id": "acc-inv", "asset_class": "EQ-FUND",
             "quantity": 10000, "unit_price": 2.0, "cost_basis": 2.0,
             "client_locked": True},
            {"id": "lot-bal-retire", "account_id": "acc-retire", "asset_class": "BAL-FUND",
             "quantity": 40000, "unit_price": 1.2, "cost_basis": 1.0},
        ],
        "goals": [
            # 购房计划提前 18 个月：2030-09 → 2027-03
            {"id": "goal-house", "name": "购房首付", "goal_type": "housing",
             "target_amount": 300000, "target_date": "2027-03-01", "priority": 1,
             "funding_account_ids": ["acc-cash", "acc-inv", "acc-edu"]},
            {"id": "goal-edu", "name": "子女教育金", "goal_type": "education",
             "target_amount": 200000, "target_date": "2032-09-01", "priority": 2,
             "funding_account_ids": ["acc-edu"]},
            {"id": "goal-retire", "name": "退休养老", "goal_type": "retirement",
             "target_amount": 1500000, "target_date": "2046-09-01", "priority": 3,
             "funding_account_ids": ["acc-retire", "acc-inv"]},
        ],
        "recurring_investments": [
            {"id": "dca-edu", "account_id": "acc-edu", "asset_class": "EQ-FUND",
             "amount": 2000, "interval_months": 1, "next_date": "2026-10-01",
             "goal_id": "goal-edu"},
            {"id": "dca-retire", "account_id": "acc-retire", "asset_class": "BAL-FUND",
             "amount": 3000, "interval_months": 1, "next_date": "2026-10-01",
             "goal_id": "goal-retire"},
        ],
        "temporary_expenses": [
            {"id": "tmp-renovation", "amount": 40000, "date": "2026-12-15",
             "description": "旧房翻新尾款", "account_id": "acc-cash"},
        ],
        "asset_classes": [
            {"code": "CASH-MM", "name": "货币基金", "risk_band": "cash",
             "expected_return": 0.015, "volatility": 0.001,
             "min_trade_unit": 1, "fee_code": "cash"},
            {"code": "BOND-IDX", "name": "债券指数基金", "risk_band": "fixed_income",
             "expected_return": 0.03, "volatility": 0.04,
             "min_trade_unit": 100, "fee_code": "fund"},
            {"code": "BAL-FUND", "name": "平衡混合基金", "risk_band": "balanced",
             "expected_return": 0.05, "volatility": 0.10,
             "min_trade_unit": 100, "fee_code": "fund"},
            {"code": "EQ-FUND", "name": "股票指数基金", "risk_band": "equity",
             "expected_return": 0.08, "volatility": 0.22,
             "min_trade_unit": 100, "fee_code": "fund"},
            {"code": "EQ-STOCK", "name": "个股", "risk_band": "equity",
             "expected_return": 0.09, "volatility": 0.30,
             "min_trade_unit": 100, "fee_code": "equity"},
        ],
        "assumptions": {
            "monthly_expenses": 15000,
            "emergency_months": 6,
            "near_term_months": 24,
            "drift_band": 0.05,
            "target_allocation": {"cash": 0.10, "fixed_income": 0.30,
                                  "balanced": 0.20, "equity": 0.40},
            "default_buy_assets": {"fixed_income": "BOND-IDX", "balanced": "BAL-FUND",
                                   "equity": "EQ-FUND", "cash": "CASH-MM"},
            "gains_tax_rates": {"taxable": 0.20, "tax_deferred": 0.0, "tax_free": 0.0},
            "cash_expected_return": 0.015,
        },
    }
