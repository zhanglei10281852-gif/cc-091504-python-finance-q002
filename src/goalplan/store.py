"""方案版本化存储。

- 每次创建或调整假设都生成新版本，旧版本快照永不修改；
- 调整假设返回受影响目标报告（ImpactReport），而不是静默替换整份方案；
- 客户确认的版本标记 confirmed，可追溯、不可变；
- 持久化到 .runtime/plans/<plan_id>/，进程重启后可恢复。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_EPSILON = 0.005  # 目标评估差异容忍度（金额占比/比率）


class PlanNotFoundError(KeyError):
    pass


class VersionNotFoundError(KeyError):
    pass


class AlreadyConfirmedError(RuntimeError):
    pass


@dataclass
class PlanVersion:
    plan_id: str
    version: int
    parent_version: int | None
    created_at: str
    author: str
    input_snapshot: dict[str, Any]
    result: dict[str, Any]
    stress: dict[str, Any] | None = None
    confirmed: bool = False
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    change_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "version": self.version,
            "parent_version": self.parent_version,
            "created_at": self.created_at,
            "author": self.author,
            "input_snapshot": self.input_snapshot,
            "result": self.result,
            "stress": self.stress,
            "confirmed": self.confirmed,
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at,
            "change_summary": self.change_summary,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlanVersion":
        return cls(**d)

    def summary(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "version": self.version,
            "parent_version": self.parent_version,
            "created_at": self.created_at,
            "author": self.author,
            "confirmed": self.confirmed,
            "confirmed_by": self.confirmed_by,
            "change_summary": self.change_summary,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def diff_goal_assessments(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """比较两版目标评估，返回受影响目标（缺口/达成率/状态发生变化）。"""
    before_map = {g["goal_id"]: g for g in before}
    after_map = {g["goal_id"]: g for g in after}
    affected: list[dict[str, Any]] = []
    for goal_id, new in after_map.items():
        old = before_map.get(goal_id)
        if old is None:
            affected.append({"goal_id": goal_id, "name": new["name"],
                             "change": "added", "after": new})
            continue
        delta_gap = new["gap"] - old["gap"]
        delta_ratio = new["funded_ratio"] - old["funded_ratio"]
        status_changed = new["status"] != old["status"]
        target_changed = (
            new["target_amount"] != old["target_amount"]
            or new["target_date"] != old["target_date"]
        )
        if abs(delta_ratio) > _EPSILON or status_changed or target_changed:
            affected.append({
                "goal_id": goal_id,
                "name": new["name"],
                "change": "modified",
                "delta_gap": round(delta_gap, 2),
                "delta_funded_ratio": round(delta_ratio, 4),
                "status_before": old["status"],
                "status_after": new["status"],
                "before": old,
                "after": new,
            })
    for goal_id, old in before_map.items():
        if goal_id not in after_map:
            affected.append({"goal_id": goal_id, "name": old["name"],
                             "change": "removed", "before": old})
    return affected


class PlanStore:
    def __init__(self, root: str | Path = ".runtime/plans"):
        self.root = Path(root)
        self._plans: dict[str, list[PlanVersion]] = {}
        self._load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _plan_dir(self, plan_id: str) -> Path:
        return self.root / plan_id

    def _load(self) -> None:
        if not self.root.exists():
            return
        for plan_dir in sorted(self.root.iterdir()):
            if not plan_dir.is_dir():
                continue
            versions = []
            for vf in sorted(plan_dir.glob("v*.json")):
                versions.append(PlanVersion.from_dict(json.loads(vf.read_text("utf-8"))))
            if versions:
                self._plans[plan_dir.name] = versions

    def _persist(self, version: PlanVersion) -> None:
        plan_dir = self._plan_dir(version.plan_id)
        plan_dir.mkdir(parents=True, exist_ok=True)
        path = plan_dir / f"v{version.version}.json"
        path.write_text(
            json.dumps(version.to_dict(), ensure_ascii=False, indent=2), "utf-8"
        )

    # ------------------------------------------------------------------
    # 版本操作
    # ------------------------------------------------------------------

    def create(
        self,
        input_snapshot: dict[str, Any],
        result: dict[str, Any],
        stress: dict[str, Any] | None,
        author: str = "system",
        plan_id: str | None = None,
    ) -> PlanVersion:
        plan_id = plan_id or f"plan-{uuid.uuid4().hex[:8]}"
        version = PlanVersion(
            plan_id=plan_id,
            version=1,
            parent_version=None,
            created_at=_now(),
            author=author,
            input_snapshot=input_snapshot,
            result=result,
            stress=stress,
            change_summary="初始方案",
        )
        self._plans[plan_id] = [version]
        self._persist(version)
        return version

    def add_version(
        self,
        plan_id: str,
        input_snapshot: dict[str, Any],
        result: dict[str, Any],
        stress: dict[str, Any] | None,
        author: str,
        change_summary: str,
    ) -> tuple[PlanVersion, dict[str, Any]]:
        """基于最新版本生成新版本，并返回受影响目标报告。"""
        versions = self._require(plan_id)
        base = versions[-1]
        version = PlanVersion(
            plan_id=plan_id,
            version=base.version + 1,
            parent_version=base.version,
            created_at=_now(),
            author=author,
            input_snapshot=input_snapshot,
            result=result,
            stress=stress,
            change_summary=change_summary,
        )
        versions.append(version)
        self._persist(version)
        impact = self.impact_report(base, version)
        return version, impact

    def confirm(self, plan_id: str, version_no: int, confirmed_by: str) -> PlanVersion:
        version = self.get(plan_id, version_no)
        if version.confirmed:
            raise AlreadyConfirmedError(
                f"版本 v{version_no} 已由 {version.confirmed_by} 确认"
            )
        version.confirmed = True
        version.confirmed_by = confirmed_by
        version.confirmed_at = _now()
        self._persist(version)
        return version

    def impact_report(self, base: PlanVersion, new: PlanVersion) -> dict[str, Any]:
        affected = diff_goal_assessments(
            base.result.get("goal_assessments", []),
            new.result.get("goal_assessments", []),
        )
        before_unmet = {c["code"] for c in base.result.get("unmet_constraints", [])}
        after_unmet = {c["code"] for c in new.result.get("unmet_constraints", [])}
        return {
            "plan_id": new.plan_id,
            "base_version": base.version,
            "new_version": new.version,
            "affected_goals": affected,
            "affected_goal_ids": [a["goal_id"] for a in affected],
            "recommendation_count_before": len(base.result.get("recommendations", [])),
            "recommendation_count_after": len(new.result.get("recommendations", [])),
            "unmet_constraints_resolved": sorted(before_unmet - after_unmet),
            "unmet_constraints_new": sorted(after_unmet - before_unmet),
            "note": "仅受影响目标需要顾问复核，其余目标评估不变",
        }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _require(self, plan_id: str) -> list[PlanVersion]:
        if plan_id not in self._plans:
            raise PlanNotFoundError(plan_id)
        return self._plans[plan_id]

    def get(self, plan_id: str, version_no: int | None = None) -> PlanVersion:
        versions = self._require(plan_id)
        if version_no is None:
            return versions[-1]
        for v in versions:
            if v.version == version_no:
                return v
        raise VersionNotFoundError(f"{plan_id} v{version_no}")

    def versions(self, plan_id: str) -> list[dict[str, Any]]:
        return [v.summary() for v in self._require(plan_id)]

    def list_plans(self) -> list[str]:
        return sorted(self._plans)
