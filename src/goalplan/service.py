"""服务门面：把引擎、压力测算与版本存储组织成 API 可调用的操作。"""

from __future__ import annotations

from datetime import date
from typing import Any

from .calendar import TradingCalendar
from .engine import RebalancingEngine
from .fees import DEFAULT_FEE_SCHEDULES
from .models import PlanInput
from .store import PlanStore, PlanVersion
from .stress import stress_test

# 假设补丁中可按 id 合并（upsert）的实体段
_ENTITY_SECTIONS = (
    "members",
    "accounts",
    "lots",
    "goals",
    "recurring_investments",
    "temporary_expenses",
    "asset_classes",
    "fee_schedules",
)


def apply_patch(snapshot: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """把顾问的假设调整合并进输入快照。

    - 实体段按 id upsert（存在的 id 更新字段，新 id 追加）；
    - assumptions 做字段级合并；
    - 不允许的键直接报错，避免静默吞掉拼写错误。
    """
    unknown = set(patch) - set(_ENTITY_SECTIONS) - {"assumptions"}
    if unknown:
        raise ValueError(f"不支持的补丁字段: {sorted(unknown)}")
    merged = dict(snapshot)
    for section in _ENTITY_SECTIONS:
        if section not in patch:
            continue
        items = patch[section]
        if not isinstance(items, list):
            raise ValueError(f"补丁段 {section} 必须是数组")
        existing = {item["id"] if "id" in item else item["code"]: item
                    for item in merged.get(section, [])}
        order = [item.get("id", item.get("code")) for item in merged.get(section, [])]
        for item in items:
            key = item.get("id", item.get("code"))
            if key is None:
                raise ValueError(f"补丁段 {section} 的条目缺少 id/code")
            if key in existing:
                existing[key] = {**existing[key], **item}
            else:
                existing[key] = item
                order.append(key)
        merged[section] = [existing[k] for k in order]
    if "assumptions" in patch:
        merged["assumptions"] = {**merged.get("assumptions", {}), **patch["assumptions"]}
    return merged


class PlanService:
    def __init__(
        self,
        storage_root: str = ".runtime/plans",
        engine: RebalancingEngine | None = None,
        calendar: TradingCalendar | None = None,
    ):
        self.calendar = calendar or TradingCalendar()
        self.engine = engine or RebalancingEngine(self.calendar)
        self.store = PlanStore(storage_root)

    # ------------------------------------------------------------------
    # 方案生命周期
    # ------------------------------------------------------------------

    def create_plan(
        self,
        payload: dict[str, Any],
        author: str = "advisor",
        as_of: date | None = None,
    ) -> dict[str, Any]:
        as_of = as_of or date.today()
        if not payload.get("accounts"):
            raise ValueError("accounts 不能为空：方案至少需要一个账户")
        if not payload.get("goals"):
            raise ValueError("goals 不能为空：方案至少需要一个目标")
        inp = self._build_input(payload)
        plan = self.engine.generate(inp, as_of)
        stress = stress_test(inp, plan)
        version = self.store.create(inp.to_dict(), plan.to_dict(), stress, author=author)
        return self._version_payload(version)

    def update_assumptions(
        self,
        plan_id: str,
        patch: dict[str, Any],
        author: str = "advisor",
        as_of: date | None = None,
        change_summary: str = "",
    ) -> dict[str, Any]:
        """调整假设：生成新版本 + 受影响目标报告，旧版本保持不变。"""
        as_of = as_of or date.today()
        base = self.store.get(plan_id)
        merged = apply_patch(base.input_snapshot, patch)
        inp = self._build_input(merged)
        plan = self.engine.generate(inp, as_of)
        stress = stress_test(inp, plan)
        summary = change_summary or self._summarize_patch(patch)
        version, impact = self.store.add_version(
            plan_id, inp.to_dict(), plan.to_dict(), stress,
            author=author, change_summary=summary,
        )
        payload = self._version_payload(version)
        payload["impact"] = impact
        return payload

    def confirm(self, plan_id: str, version_no: int, confirmed_by: str) -> dict[str, Any]:
        version = self.store.confirm(plan_id, version_no, confirmed_by)
        return version.summary()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_plan(self, plan_id: str, version_no: int | None = None) -> dict[str, Any]:
        return self._version_payload(self.store.get(plan_id, version_no))

    def list_versions(self, plan_id: str) -> list[dict[str, Any]]:
        return self.store.versions(plan_id)

    def get_stress(self, plan_id: str, version_no: int | None = None) -> dict[str, Any]:
        version = self.store.get(plan_id, version_no)
        return version.stress or {}

    def market_status(self, day: date | None = None) -> dict[str, Any]:
        return self.calendar.status(day or date.today())

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _build_input(self, payload: dict[str, Any]) -> PlanInput:
        """补默认费用样例后构建强类型输入。"""
        data = dict(payload)
        if not data.get("fee_schedules"):
            data["fee_schedules"] = [f.to_dict() for f in DEFAULT_FEE_SCHEDULES.values()]
        return PlanInput.from_dict(data)

    def _version_payload(self, version: PlanVersion) -> dict[str, Any]:
        return {
            "plan_id": version.plan_id,
            "version": version.version,
            "parent_version": version.parent_version,
            "created_at": version.created_at,
            "author": version.author,
            "confirmed": version.confirmed,
            "confirmed_by": version.confirmed_by,
            "change_summary": version.change_summary,
            "result": version.result,
            "stress": version.stress,
        }

    @staticmethod
    def _summarize_patch(patch: dict[str, Any]) -> str:
        parts = []
        for section, items in patch.items():
            if section == "assumptions":
                parts.append(f"假设字段 {sorted(items)}")
            else:
                ids = [i.get("id", i.get("code", "?")) for i in items]
                parts.append(f"{section}: {', '.join(ids)}")
        return "调整 " + "；".join(parts) if parts else "假设调整"
