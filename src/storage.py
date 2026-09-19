"""方案与版本持久化（写入 .runtime/，JSON 原子落盘）。

版本链：
- 每次生成调仓报告得到 draft 版本，记录父版本与所用假设快照；
- 客户确认后版本变为 confirmed，内容不可变；
- 再次推演基于当前工作副本，parent 指向最近版本，形成可追溯链。

时间区分：plan.as_of 为业务发生日；created_at 为系统接收时间（UTC）。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

RUNTIME_DIR = Path(os.getenv("RUNTIME_DIR",
                             str(Path(__file__).resolve().parents[1]
                                 / ".runtime")))
PLANS_DIR = RUNTIME_DIR / "plans"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


class VersionStore:
    def __init__(self, root: Path = PLANS_DIR) -> None:
        self.root = root
        self._lock = threading.Lock()

    # ---- 方案工作副本 ----
    def _plan_dir(self, plan_id: str) -> Path:
        return self.root / plan_id

    def _plan_path(self, plan_id: str) -> Path:
        return self._plan_dir(plan_id) / "plan.json"

    def _versions_dir(self, plan_id: str) -> Path:
        return self._plan_dir(plan_id) / "versions"

    def plan_exists(self, plan_id: str) -> bool:
        return self._plan_path(plan_id).exists()

    def save_plan(self, plan_payload: dict) -> None:
        with self._lock:
            _atomic_write(self._plan_path(plan_payload["id"]), plan_payload)

    def load_plan(self, plan_id: str) -> dict:
        path = self._plan_path(plan_id)
        if not path.exists():
            raise KeyError(f"方案不存在: {plan_id}")
        return _read(path)

    def list_plans(self) -> list[dict]:
        if not self.root.exists():
            return []
        out = []
        for d in sorted(self.root.iterdir()):
            p = d / "plan.json"
            if p.exists():
                payload = _read(p)
                out.append({"id": payload["id"],
                            "name": payload.get("household", {}).get("name"),
                            "as_of": payload.get("as_of"),
                            "updated_at": datetime.fromtimestamp(
                                p.stat().st_mtime, timezone.utc
                            ).isoformat(timespec="seconds"),
                            "version_count": len(list(
                                (d / "versions").glob("*.json")))
                            if (d / "versions").exists() else 0})
        return out

    # ---- 版本 ----
    def _version_path(self, plan_id: str, version_id: str) -> Path:
        return self._versions_dir(plan_id) / f"{version_id}.json"

    def head_version_id(self, plan_id: str) -> str | None:
        vdir = self._versions_dir(plan_id)
        if not vdir.exists():
            return None
        versions = [p.stem for p in vdir.glob("*.json")]
        if not versions:
            return None
        return sorted(versions)[-1]

    def add_version(self, plan_id: str, plan_payload: dict,
                    report: dict, *, label: str = "",
                    patch: dict | None = None,
                    created_by: str = "advisor",
                    source_version_id: str | None = None) -> dict:
        """落盘新的 draft 版本。confirmed 版本永不被覆盖。"""
        with self._lock:
            parent = source_version_id or self.head_version_id(plan_id)
            # 工作副本推进
            _atomic_write(self._plan_path(plan_id), plan_payload)
            seq = len(list(self._versions_dir(plan_id).glob("*.json"))) + 1 \
                if self._versions_dir(plan_id).exists() else 1
            version_id = f"v{seq:03d}-{uuid.uuid4().hex[:8]}"
            payload = {
                "version_id": version_id,
                "plan_id": plan_id,
                "sequence": seq,
                "status": "draft",
                "label": label,
                "parent_version_id": parent,
                "created_at": utc_now(),
                "created_by": created_by,
                "business_date": plan_payload.get("as_of"),
                "patch": patch,
                "plan_snapshot": plan_payload,
                "report": report,
                "confirmation": None,
            }
            _atomic_write(self._version_path(plan_id, version_id), payload)
            return self._version_summary(payload)

    def get_version(self, plan_id: str, version_id: str) -> dict:
        path = self._version_path(plan_id, version_id)
        if not path.exists():
            raise KeyError(f"版本不存在: {version_id}")
        return _read(path)

    def confirm_version(self, plan_id: str, version_id: str,
                        confirmed_by: str = "client",
                        note: str = "") -> dict:
        with self._lock:
            path = self._version_path(plan_id, version_id)
            if not path.exists():
                raise KeyError(f"版本不存在: {version_id}")
            payload = _read(path)
            if payload["status"] == "confirmed" \
                    and payload["confirmation"]["confirmed_by"] != confirmed_by:
                raise ValueError("版本已由其他确认人确认，不可更改")
            if payload["status"] != "confirmed":
                payload["status"] = "confirmed"
                payload["confirmation"] = {
                    "confirmed_at": utc_now(),
                    "confirmed_by": confirmed_by,
                    "note": note,
                }
                _atomic_write(path, payload)
            return self._version_summary(payload)

    def list_versions(self, plan_id: str) -> list[dict]:
        vdir = self._versions_dir(plan_id)
        if not vdir.exists():
            return []
        out = []
        for p in sorted(vdir.glob("*.json")):
            payload = _read(p)
            out.append(self._version_summary(payload))
        return out

    @staticmethod
    def _version_summary(payload: dict) -> dict:
        return {
            "version_id": payload["version_id"],
            "plan_id": payload["plan_id"],
            "sequence": payload["sequence"],
            "status": payload["status"],
            "label": payload.get("label", ""),
            "parent_version_id": payload.get("parent_version_id"),
            "created_at": payload["created_at"],
            "created_by": payload.get("created_by"),
            "business_date": payload.get("business_date"),
            "has_patch": bool(payload.get("patch")),
            "confirmation": payload.get("confirmation"),
            "recommendation_count": len(
                payload.get("report", {}).get("recommendations", [])),
            "constraint_count": len(
                payload.get("report", {}).get("constraints", [])),
        }

    def ancestry(self, plan_id: str, version_id: str) -> list[str]:
        chain: list[str] = []
        cur: str | None = version_id
        seen: set[str] = set()
        while cur and cur not in seen:
            seen.add(cur)
            payload = self.get_version(plan_id, cur)
            chain.append(cur)
            cur = payload.get("parent_version_id")
        return chain
