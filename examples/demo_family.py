"""演示：购房计划提前 18 个月后的目标组合再平衡。

流程：
1. 顾问为客户建立方案（2026-09-19 周六，市场休市 → 只形成计划）；
2. 查看调仓建议：每笔解决了哪个缺口、税费与现金变化；
3. 查看未满足约束（禁售/待交收/客户锁定/缺口）；
4. 查看压力情景下各目标到期达成情况；
5. 顾问调整假设（购房预算上调）→ 只报告受影响目标；
6. 客户确认新版本，历史版本保持可追溯。

运行：python3 examples/demo_family.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from goalplan.service import PlanService  # noqa: E402
from sample_data import family_payload  # noqa: E402

AS_OF = date(2026, 9, 19)  # 周六


def show(title: str) -> None:
    print(f"\n{'=' * 20} {title} {'=' * 20}")


def main() -> None:
    tmp = tempfile.TemporaryDirectory()
    service = PlanService(storage_root=tmp.name)

    show("1. 建立方案（休市日，只形成计划）")
    created = service.create_plan(family_payload(), author="advisor-wang", as_of=AS_OF)
    plan_id = created["plan_id"]
    result = created["result"]
    print(f"方案 {plan_id} v{created['version']}  状态: {result['status']}"
          f"（计划交易日 {result['planned_trade_date']}）")
    print(f"现金: {result['cash_projection']['start_cash']:,.0f}"
          f" → {result['cash_projection']['end_cash']:,.0f}"
          f"（应急目标 {result['cash_projection']['emergency_target']:,.0f}）")

    show("2. 调仓建议（每笔对应哪个缺口）")
    for rec in result["recommendations"]:
        side = "卖出" if rec["side"] == "sell" else "买入"
        purposes = "；".join(
            p.get("goal_name") or f"风险带 {p.get('band')} 再平衡" for p in rec["purposes"]
        )
        print(f"  {rec['rec_id']} {side} {rec['asset_class']} "
              f"{rec['quantity']:,.0f} 份 @ {rec['unit_price']}"
              f"  金额 {rec['gross_amount']:,.0f}"
              f"  费 {rec['estimated_fee']:,.2f} 税 {rec['estimated_tax']:,.2f}"
              f"  现金净变动 {rec['net_cash_delta']:,.0f}")
        print(f"      → {purposes}｜{rec['rationale']}"
              f"｜成交后现金 {rec['cash_after']:,.0f}")

    show("3. 目标评估")
    for g in result["goal_assessments"]:
        tag = "近期" if g["near_term"] else "远期"
        print(f"  [{tag}] {g['name']}（{g['target_date']}，优先级 {g['priority']}）"
              f"  目标 {g['target_amount']:,.0f}"
              f"  到期测算 {g['projected_value']:,.0f}"
              f"  缺口 {g['gap']:,.0f}  状态 {g['status']}")

    show("4. 未满足约束（不得绕过的红线）")
    for c in result["unmet_constraints"]:
        print(f"  [{c['severity']}] {c['code']}: {c['message']}")

    show("5. 压力情景下的到期达成")
    for scenario in created["stress"]["scenarios"]:
        marks = "、".join(
            f"{g['name']} {g['achievement_ratio']:.0%}{'✓' if g['met'] else '✗'}"
            for g in scenario["goals"]
        )
        print(f"  {scenario['name']:<14} {marks}")

    show("6. 顾问调整假设：购房预算 30 万 → 36 万")
    updated = service.update_assumptions(
        plan_id,
        {"goals": [{"id": "goal-house", "target_amount": 360000}]},
        author="advisor-wang",
        as_of=AS_OF,
        change_summary="客户决定提高购房预算",
    )
    impact = updated["impact"]
    print(f"生成 v{updated['version']}（父版本 v{impact['base_version']}），"
          f"受影响目标: {impact['affected_goal_ids']}")
    for a in impact["affected_goals"]:
        print(f"  {a['name']}: 缺口 {a['before']['gap']:,.0f} → {a['after']['gap']:,.0f}"
              f"  状态 {a['status_before']} → {a['status_after']}")
    print("  其余目标评估不变，无需复核。")

    show("7. 客户确认与版本追溯")
    service.confirm(plan_id, 2, confirmed_by="客户张先生")
    for v in service.list_versions(plan_id):
        flag = "已确认" if v["confirmed"] else "草稿"
        print(f"  v{v['version']} [{flag}] {v['change_summary']}"
          f"（{v['author']}，{v['created_at']}）")

    v1 = service.get_plan(plan_id, 1)
    house_v1 = next(g for g in v1["result"]["goal_assessments"]
                    if g["goal_id"] == "goal-house")
    print(f"  追溯 v1 购房目标额仍为 {house_v1['target_amount']:,.0f}，未被新版本覆盖")

    show("完成")
    print(json.dumps({"plan_id": plan_id}, ensure_ascii=False))
    tmp.cleanup()


if __name__ == "__main__":
    main()
