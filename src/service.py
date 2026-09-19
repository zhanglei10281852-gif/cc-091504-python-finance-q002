"""应用服务层：组合参考数据、方案、调仓引擎、what-if 与版本存储。"""
from __future__ import annotations

from datetime import date

from market import MarketData, load_seed_plan
from models import Plan, parse_date
from rebalance import generate
from scenarios import PatchError, what_if
from storage import VersionStore


class Service:
    def __init__(self) -> None:
        self.market = MarketData()
        self.store = VersionStore()

    # ---- 参考数据 ----
    def reference(self) -> dict:
        m = self.market
        return {
            "currency": m.currency,
            "quote_date": m.quote_date.isoformat(),
            "asset_classes": list(m.classes.values()),
            "instruments": [
                {"id": i.id, "name": i.name, "asset_class": i.asset_class,
                 "last_price": str(i.last_price), "lot_size": i.lot_size,
                 "min_trade_amount": str(i.min_trade_amount),
                 "settlement_days": i.settlement_days}
                for i in m.instruments.values()],
            "fees": {
                "commission_bp": str(m.fees.commission_bp * 10000),
                "min_commission": str(m.fees.min_commission),
                "stamp_duty_bp_sell": str(m.fees.stamp_duty_bp_sell * 10000),
                "capital_gains_tax": [
                    {"tax_status": k,
                     "short_rate": str(v.short_rate),
                     "long_rate": str(v.long_rate),
                     "long_holding_days": v.long_holding_days}
                    for k, v in m.fees.cgt.items()]},
            "glide_path": m.glide,
            "scenarios": [
                {"id": s.id, "name": s.name,
                 "returns": {k: str(v) for k, v in s.returns.items()},
                 "contribution_haircut": str(s.contribution_haircut),
                 "target_multiplier": str(s.target_multiplier)}
                for s in m.scenarios.values()],
            "calendar": {
                "holidays": sorted(d.isoformat() for d in m.holidays),
                "today_open": m.is_open_on(date.today())},
        }

    # ---- 方案 ----
    def ensure_seed(self) -> dict:
        payload = load_seed_plan()
        if not self.store.plan_exists(payload["id"]):
            self.store.save_plan(payload)
        return payload

    def load_plan(self, plan_id: str) -> Plan:
        return Plan.from_dict(self.store.load_plan(plan_id))

    def save_plan_payload(self, payload: dict) -> Plan:
        plan = Plan.from_dict(payload)
        self.store.save_plan(plan.to_dict())
        return plan

    # ---- 调仓 ----
    def rebalance(self, plan_id: str, trade_date: str | None,
                  label: str = "draft") -> dict:
        plan = self.load_plan(plan_id)
        td = parse_date(trade_date) if trade_date else None
        report = generate(plan, self.market, td)
        version = self.store.add_version(
            plan_id, plan.to_dict(), report, label=label,
            created_by="advisor")
        report["version"] = version
        return report

    # ---- 假设变更 ----
    def what_if(self, plan_id: str, patch: dict,
                trade_date: str | None, save: bool = False,
                label: str = "what-if") -> dict:
        plan = self.load_plan(plan_id)
        td = parse_date(trade_date) if trade_date else None
        result = what_if(plan, self.market, patch, td)
        if save:
            new_payload = result["patched_plan"]
            version = self.store.add_version(
                plan_id, new_payload, result["after_report"],
                label=label, patch=patch, created_by="advisor")
            result["version"] = version
        return result

    # ---- 版本 ----
    def versions(self, plan_id: str) -> dict:
        return {"plan_id": plan_id,
                "versions": self.store.list_versions(plan_id)}

    def version(self, plan_id: str, version_id: str) -> dict:
        return self.store.get_version(plan_id, version_id)

    def confirm(self, plan_id: str, version_id: str,
                body: dict) -> dict:
        return self.store.confirm_version(
            plan_id, version_id,
            confirmed_by=body.get("confirmed_by", "client"),
            note=body.get("note", ""))

    def ancestry(self, plan_id: str, version_id: str) -> dict:
        return {"plan_id": plan_id, "version_id": version_id,
                "ancestry": self.store.ancestry(plan_id, version_id)}


SERVICE = Service()
