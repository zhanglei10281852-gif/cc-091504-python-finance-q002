# 家庭目标组合再平衡服务

面向家庭多目标资金（应急、购房、教育、养老）的目标组合再平衡后端。
纯 Python 3.11 标准库实现，无第三方依赖；金额全程 `Decimal`，持久化使用
`.runtime/` 下的原子 JSON 文件。

## 解决的问题

购房计划提前后，短期现金缺口与长期配置目标会相互挤占。服务把家庭成员、
目标期限、资金优先级、账户税务属性、持仓批次（含客户锁定/禁售）、定投与
临时支出组织在同一套方案中，输出可解释、可追溯的调仓建议：

1. **优先级瀑布**：先补应急现金（即时已交收口径），再处理近期目标
   （账户内降风险、债券按需变现、待交收提示），最后仅在偏离超过
   **漂移带**时调整长期配置；近期目标资金不会被长期目标挤占，
   受限账户（如养老金）不作为其他目标的变现来源。
2. **硬约束不可绕过**：客户锁定批次（`locked`）、禁售批次
   （`lockup_until`）、最低交易单位/最低成交金额、待交收资金交收日、
   市场休市——任何无法满足的约束只形成提示，不会产生违规建议。
3. **休市只计划**：请求日落入周末/节假日时，全部建议状态为
   `planned_market_closed`，假定成交日顺延至下一交易日，价格需当日重估。
4. **假设变更影响面**：顾问修改某假设后只重算受影响目标（what-if），
   整份方案不会被静默替换。
5. **版本可追溯**：每次推演形成带父版本链的版本快照，客户确认后
   不可变。
6. **解释充分**：每笔建议说明解决哪个缺口、税费/现金变化与交收时点；
   每个目标给出调仓前后缺口、资金构成与五个压力情景下的到期达成率。

## 目录

```
reference/            样例参考数据（可替换为正式业务数据源）
  asset_classes.json  资产分类、风险滑行路径、漂移带
  instruments.json    工具行情、最低交易单位、交收天数
  calendar.json       交易日历（周末 + 节假日）
  fees.json           佣金/印花税/分税务属性与持有期的资本利得税
  stress.json         压力情景（权益急跌、利率上行、收入冲击、通胀）
  seed_plan.json      示例家庭：购房提前至 2027-03，教育金持有波动股票
src/
  models.py           领域模型（Decimal 金额、批次/待交收/定投/支出）
  market.py           参考数据、交易日历、T+n 交收推算
  projection.py       目标缺口分析与压力情景到期测算
  rebalance.py        优先级瀑布调仓引擎（税费、整手、约束归集）
  scenarios.py        假设补丁与受影响目标分析（what-if）
  storage.py          方案工作副本与版本链持久化
  service.py          服务编排
  app.py              HTTP 路由（标准库）
tests/                44 项单元与端到端测试
```

## 运行

```bash
python3 src/index.py            # 默认 0.0.0.0:8000
python3 -m unittest discover -s tests
docker compose up --build
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/reference` | 分类、工具、费用税率、滑行路径、交易日历、压力情景 |
| POST | `/plans/seed` | 载入示例家庭 |
| GET | `/plans` / `/plans/{id}` | 方案列表 / 详情 |
| PUT | `/plans/{id}` | 更新方案（稳定标识校验引用完整性） |
| POST | `/plans/{id}/rebalance?trade_date=YYYY-MM-DD` | 生成调仓报告并落 draft 版本 |
| POST | `/plans/{id}/what-if?save=true` | 假设补丁，仅返回受影响目标；`save=true` 时另存版本 |
| GET | `/plans/{id}/versions` | 版本列表 |
| GET | `/plans/{id}/versions/{vid}` | 版本快照（方案 + 报告 + 确认信息） |
| POST | `/plans/{id}/versions/{vid}/confirm` | 客户确认（确认后不可变） |
| GET | `/plans/{id}/versions/{vid}/ancestry` | 版本祖先链 |

### what-if 补丁示例

```json
{
  "goals": {"g_housing": {"target_date": "2028-09-01"}},
  "contributions": {"c_housing": {"amount": "12000.00"}},
  "assumptions": {"emergency_months": 6}
}
```

### 报告关键字段

- `recommendations[]`：`sell` / `buy` / `cash_transfer` / `funding_action`，
  含 `rationale`（解决哪个缺口）、`purpose`、`linked_recommendation_ids`、
  `gross_amount`、`commission`、`stamp_duty`、`capital_gains_tax`
  （含税务属性、持有天数、适用税率明细）、`cash_change` 与
  `cash_change_settled_at`、休市时的计划状态。
- `gap_resolution[]`：每目标调仓前后缺口、缩减额、残余缺口、归属费用税款
  与对应建议编号。
- `constraints[]`：未满足约束（资金缺口、锁定/禁售阻断、低于最小交易单位、
  无现金回补、市场休市），含被阻断批次与所需月供等可执行提示。
- `stress_before` / `stress_after`：五个情景 × 四个目标的到期价值、
  缺口与达成率。
- `cash_impact`：分账户费用/税款/现金变化，以及家庭总财富守恒校验
  （调仓不创造价值：`wealth_change + fees + tax = 0`）。

## 口径说明

- 应急目标只承认即时已交收现金；待交收资金单列并在交收日后确认。
- 近期目标（3 年内）按确定性现金口径：债券计提 5% 流动性折扣、股票按
  期限线性确认（1 年内不确认），以避免用波动资产瞬时市值覆盖短期缺口；
  长期目标按风险区间与滑行路径确定目标比例。
- 压力情景对权益/债券/货币/现金分别施加年化冲击（首个年度冲击后恢复），
  收入冲击折减定投，通胀情景放大目标名义额。
