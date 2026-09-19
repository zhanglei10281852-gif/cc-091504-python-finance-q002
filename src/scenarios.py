"""假设变更的影响面分析（what-if）。

顾问调整某个假设时，系统只重算并标注受影响目标，而不是静默替换整份方案。
补丁格式：
{
  "assumptions": {"emergency_months": 6, "monthly_household_expense": "33000",
                  "default_risk_band": "balanced"},
  "goals": {"g_housing": {"target_date": "2027-03-01",
                          "target_amount": "800000"}},
  "contributions": {"c_housing": {"amount": "9000.00"}},
  "expenses": {"e_housing_tax": {"amount": "55000.00"}}
}
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from market import MarketData
from models import D, Plan, cents, d_str, parse_date
from projection import analyze, years_between
from rebalance import generate

PATCHABLE_GOAL_FIELDS = {"target_amount", "target_date", "risk_band",
                         "priority", "name"}
PATCHABLE_ASSUMPTIONS = {"monthly_household_expense", "emergency_months",
                         "default_risk_band"}
PATCHABLE_CONTRIB_FIELDS = {"amount", "end_date"}
PATCHABLE_EXPENSE_FIELDS = {"amount", "due_date"}


class PatchError(ValueError):
    pass


def apply_patch(plan: Plan, patch: dict) -> tuple[Plan, list[str]]:
    """返回（打补丁后的方案副本, 直接受影响目标 id 列表）。"""
    new = _clone_plan(plan)
    affected: set[str] = set()

    for key, value in (patch.get("assumptions") or {}).items():
        if key not in PATCHABLE_ASSUMPTIONS:
            raise PatchError(f"假设字段不可修改: {key}")
        _set_assumption(new, key, value)
        if key in ("monthly_household_expense", "emergency_months"):
            em = next((g.id for g in new.goals if g.type == "emergency"), None)
            if em:
                affected.add(em)
        elif key == "default_risk_band":
            affected.update(g.id for g in new.goals if g.type != "emergency")

    for gid, fields_ in (patch.get("goals") or {}).items():
        goal = _find(new.goals, gid, "目标")
        for k, v in fields_.items():
            if k not in PATCHABLE_GOAL_FIELDS:
                raise PatchError(f"目标字段不可修改: {k}")
            if k == "target_amount":
                goal.target_amount = D(v)
            elif k == "target_date":
                goal.target_date = parse_date(v)
            else:
                setattr(goal, k, v)
        affected.add(gid)
        # 近期目标期限变化会改变应急储备可动用的捐赠账户集合
        if "target_date" in fields_:
            em = next((g.id for g in new.goals if g.type == "emergency"), None)
            if em:
                affected.add(em)

    for cid, fields_ in (patch.get("contributions") or {}).items():
        con = _find(new.contributions, cid, "定投")
        for k, v in fields_.items():
            if k not in PATCHABLE_CONTRIB_FIELDS:
                raise PatchError(f"定投字段不可修改: {k}")
            if k == "amount":
                con.amount = D(v)
            elif k == "end_date":
                con.end_date = parse_date(v)
        affected.add(con.goal_id)

    for eid, fields_ in (patch.get("expenses") or {}).items():
        ex = _find(new.expenses, eid, "支出")
        for k, v in fields_.items():
            if k not in PATCHABLE_EXPENSE_FIELDS:
                raise PatchError(f"支出字段不可修改: {k}")
            if k == "amount":
                ex.amount = D(v)
            elif k == "due_date":
                ex.due_date = parse_date(v)
        affected.add(ex.goal_id or
                     next(g.id for g in new.goals if g.type == "emergency"))

    new.validate_refs()
    return new, sorted(affected)


def what_if(plan: Plan, market: MarketData, patch: dict,
            trade_date: date | None = None) -> dict:
    new_plan, affected = apply_patch(plan, patch)
    before_report = generate(plan, market, trade_date)
    after_report = generate(new_plan, market, trade_date)

    before_rows = {r["goal_id"]: r for r
                   in before_report["goal_analysis_after"]}
    after_rows = {r["goal_id"]: r for r
                  in after_report["goal_analysis_after"]}

    goal_diffs = []
    for gid in affected:
        b0 = before_rows.get(gid)
        a1 = after_rows.get(gid)
        if not b0 or not a1:
            continue
        goal_diffs.append({
            "goal_id": gid, "goal_name": a1["name"],
            "affected_fields": _affected_fields(patch, gid, new_plan),
            "gap_before": b0["gap"], "gap_after": a1["gap"],
            "funded_ratio_before": b0["funded_ratio"],
            "funded_ratio_after": a1["funded_ratio"],
            "target_mix_before": b0["target_mix"],
            "target_mix_after": a1["target_mix"],
            "projected_before": b0["projected_maturity_value"],
            "projected_after": a1["projected_maturity_value"],
            "gap_change": cents(D(a1["gap"]) - D(b0["gap"])),
            "new_constraints": [c for c in a1["constraints"]
                                if c not in b0["constraints"]],
        })

    affected_set = set(affected)
    return {
        "plan_id": plan.id,
        "patch": patch,
        "affected_goal_ids": affected,
        "impact_scope_note": ("仅重算并展示受影响目标；未列出的目标沿用原分析，"
                              "整份方案不会被静默替换"),
        "goal_diffs": goal_diffs,
        "recommendations_affected": [
            r for r in after_report["recommendations"]
            if r.get("goal_id") in affected_set],
        "constraints_affected": [
            c for c in after_report["constraints"]
            if c.get("goal_id") in affected_set or c.get("goal_id") is None],
        "stress_after_affected": [
            s for s in after_report["stress_after"]
            if s["goal_id"] in affected_set],
        "after_report": after_report,
        "patched_plan": new_plan.to_dict(),
    }


def _affected_fields(patch: dict, gid: str, new_plan: Plan) -> list[str]:
    out = []
    if gid in (patch.get("goals") or {}):
        out.extend(f"goal.{k}" for k in patch["goals"][gid])
    for cid, fields_ in (patch.get("contributions") or {}).items():
        con = next((c for c in new_plan.contributions if c.id == cid), None)
        if con and con.goal_id == gid:
            out.extend(f"contribution.{cid}.{k}" for k in fields_)
    for eid, fields_ in (patch.get("expenses") or {}).items():
        ex = next((e for e in new_plan.expenses if e.id == eid), None)
        em = next((g for g in new_plan.goals if g.type == "emergency"), None)
        if ex and (ex.goal_id == gid
                   or (ex.goal_id is None and em and em.id == gid)):
            out.extend(f"expense.{eid}.{k}" for k in fields_)
    if any(k in ("emergency_months", "monthly_household_expense")
           for k in (patch.get("assumptions") or {})):
        if next((g.type for g in new_plan.goals if g.id == gid), None) \
                == "emergency":
            out.append("assumptions.emergency")
    if "default_risk_band" in (patch.get("assumptions") or {}):
        out.append("assumptions.default_risk_band")
    return out


def _set_assumption(plan: Plan, key: str, value: object) -> None:
    if key == "monthly_household_expense":
        plan.assumptions.monthly_household_expense = D(value)
    elif key == "emergency_months":
        plan.assumptions.emergency_months = int(value)  # type: ignore[arg-type]
        target = plan.assumptions.emergency_months * \
            plan.assumptions.monthly_household_expense
        em = next((g for g in plan.goals if g.type == "emergency"), None)
        if em:
            em.target_amount = target
    elif key == "default_risk_band":
        band = str(value)
        plan.assumptions.default_risk_band = band
        for g in plan.goals:
            if g.type != "emergency":
                g.risk_band = band


def _find(items: list, item_id: str, label: str):
    for item in items:
        if item.id == item_id:
            return item
    raise PatchError(f"{label}不存在: {item_id}")


def _clone_plan(plan: Plan) -> Plan:
    return Plan.from_dict(plan.to_dict())
