# 家庭目标规划服务

面向家庭资金目标与资产配置决策的 Python 后端服务（纯标准库，无第三方依赖）。

把家庭成员、目标期限、资金优先级、账户税务属性、持仓批次、定投与临时支出组织在同一套方案中，输出可解释的调仓建议：先保证应急现金与近期目标，再在漂移带内处理长期配置。禁售批次、待交收资金、客户锁定资产与最低交易单位构成不可绕过的约束；市场休市时只形成计划、不假定成交。顾问调整假设会生成新版本并只报告受影响目标，客户确认的版本保持可追溯。

## 运行

需要 Python 3.11 或更高版本：

```bash
python3 src/index.py
```

服务默认监听 `8000` 端口，`GET /health` 确认进程状态。执行测试与场景演示：

```bash
python3 -m unittest discover -s tests
python3 examples/demo_family.py
```

也可以运行 `docker compose up --build` 启动容器。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/api/market/status?date=YYYY-MM-DD` | 交易日历：是否开市、下一交易日、T+1 交收日 |
| POST | `/api/plans` | 创建方案：返回调仓建议、目标评估、未满足约束、压力情景 |
| GET | `/api/plans/{plan_id}` | 最新版本 |
| GET | `/api/plans/{plan_id}/versions` | 版本列表（含确认状态） |
| GET | `/api/plans/{plan_id}/versions/{n}` | 指定历史版本 |
| POST | `/api/plans/{plan_id}/assumptions` | 调整假设 → 新版本 + 受影响目标报告 |
| POST | `/api/plans/{plan_id}/confirm` | 客户确认某版本（`{version, confirmed_by}`） |
| GET | `/api/plans/{plan_id}/stress` | 最新版本的压力情景到期达成 |

每笔调仓建议都解释：解决了哪个缺口（`purposes`）、预估费用与税费、现金净变动与成交后现金、计划交易日与是否可执行。方案版本持久化在 `.runtime/plans/`（已加入 `.gitignore`）。

### 创建方案负载

```json
{
  "as_of": "2026-09-19",
  "author": "advisor-wang",
  "members": [{"id": "m1", "name": "张先生", "relation": "self"}],
  "accounts": [{"id": "a1", "owner_id": "m1", "tax_treatment": "taxable",
                "cash_balance": 80000,
                "pending_cash": [{"amount": 30000, "settle_date": "2026-09-22"}]}],
  "lots": [{"id": "l1", "account_id": "a1", "asset_class": "EQ-FUND",
            "quantity": 30000, "unit_price": 2.0, "cost_basis": 1.5,
            "status": "tradable"}],
  "goals": [{"id": "g1", "name": "购房首付", "goal_type": "housing",
             "target_amount": 300000, "target_date": "2027-03-01",
             "priority": 1, "funding_account_ids": ["a1"]}],
  "recurring_investments": [{"id": "d1", "account_id": "a1", "asset_class": "EQ-FUND",
                             "amount": 2000, "interval_months": 1,
                             "next_date": "2026-10-01", "goal_id": "g1"}],
  "temporary_expenses": [{"id": "t1", "amount": 40000, "date": "2026-12-15",
                          "account_id": "a1"}],
  "asset_classes": [{"code": "EQ-FUND", "name": "股票指数基金", "risk_band": "equity",
                     "expected_return": 0.08, "volatility": 0.22,
                     "min_trade_unit": 100, "fee_code": "fund"}],
  "assumptions": {"monthly_expenses": 15000, "emergency_months": 6,
                  "near_term_months": 24, "drift_band": 0.05,
                  "target_allocation": {"cash": 0.1, "fixed_income": 0.3,
                                        "balanced": 0.2, "equity": 0.4},
                  "default_buy_assets": {"equity": "EQ-FUND"}}
}
```

完整可运行样例见 `tests/sample_data.py`（即演示脚本使用的家庭场景）。

### 调整假设

```json
POST /api/plans/{plan_id}/assumptions
{
  "patch": {"goals": [{"id": "g1", "target_amount": 360000}],
            "assumptions": {"emergency_months": 9}},
  "author": "advisor-wang",
  "change_summary": "客户提高购房预算"
}
```

实体段按 `id`（资产/费用按 `code`）upsert，`assumptions` 字段级合并；响应中的 `impact.affected_goals` 只列出评估发生变化的目标，其余目标无需复核。

## 引擎规则（样例口径，均可配置）

1. **应急现金**：目标 = 月支出 × 覆盖月数；缺口优先赎回现金类资产补足。
2. **近期目标**（默认 24 个月内）：应急之外的现金先分配给目标；剩余缺口把归属资产降险至安全档（12 个月内只认现金档，24 个月内含固收档），按目标日期与优先级依次筹集；卖出按税务效率排序（应税账户优先、亏损批次优先、费率低者优先）。
3. **长期配置**：风险带权重超出漂移带才调仓；现金超配不触发卖出（通常是目标预留），低配风险带用可投资现金补足。
4. **约束红线**：禁售批次、待交收批次、客户锁定资产不出现在任何建议中；卖出数量取整到最低交易单位；休市日方案标记 `planned_only`，计划交易日顺延。

费用与税率为演示样例（`src/goalplan/fees.py`）：佣金有最低收费、印花税仅卖出收取、应税账户按盈利计提资本利得税，递延/免税账户当期不计。
