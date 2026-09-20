"""HTTP 层：标准库实现的 JSON API。

路由：
  GET  /health                                  健康检查
  GET  /api/market/status?date=YYYY-MM-DD       交易日历状态
  POST /api/plans                               创建方案（含调仓建议与压力测算）
  GET  /api/plans/{plan_id}                     最新版本
  GET  /api/plans/{plan_id}/versions            版本列表（可追溯）
  GET  /api/plans/{plan_id}/versions/{n}        指定版本
  POST /api/plans/{plan_id}/assumptions         调整假设 → 新版本 + 受影响目标
  POST /api/plans/{plan_id}/confirm             客户确认某版本
  GET  /api/plans/{plan_id}/stress              最新版本压力情景
"""

from __future__ import annotations

import json
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from goalplan.service import PlanService
from goalplan.store import (
    AlreadyConfirmedError,
    PlanNotFoundError,
    VersionNotFoundError,
)

SERVICE_NAME = '家庭目标规划服务'


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


Handler = Callable[["RequestContext"], tuple[int, dict[str, Any]]]


class RequestContext:
    def __init__(self, service: PlanService, query: dict[str, list[str]],
                 body: dict[str, Any] | None, params: dict[str, str]):
        self.service = service
        self.query = query
        self.body = body or {}
        self.params = params


def _parse_day(raw: str | None) -> date | None:
    if not raw:
        return None
    return date.fromisoformat(raw)


def _routes() -> list[tuple[str, re.Pattern[str], Handler]]:
    def health(ctx: RequestContext) -> tuple[int, dict[str, Any]]:
        return 200, health_payload()

    def market_status(ctx: RequestContext) -> tuple[int, dict[str, Any]]:
        day = _parse_day(ctx.query.get("date", [None])[0])
        return 200, ctx.service.market_status(day)

    def create_plan(ctx: RequestContext) -> tuple[int, dict[str, Any]]:
        author = ctx.body.get("author", "advisor")
        as_of = _parse_day(ctx.body.get("as_of"))
        return 201, ctx.service.create_plan(ctx.body, author=author, as_of=as_of)

    def get_plan(ctx: RequestContext) -> tuple[int, dict[str, Any]]:
        return 200, ctx.service.get_plan(ctx.params["plan_id"])

    def list_versions(ctx: RequestContext) -> tuple[int, dict[str, Any]]:
        return 200, {"plan_id": ctx.params["plan_id"],
                     "versions": ctx.service.list_versions(ctx.params["plan_id"])}

    def get_version(ctx: RequestContext) -> tuple[int, dict[str, Any]]:
        return 200, ctx.service.get_plan(
            ctx.params["plan_id"], int(ctx.params["version"])
        )

    def update_assumptions(ctx: RequestContext) -> tuple[int, dict[str, Any]]:
        patch = ctx.body.get("patch", ctx.body)
        return 201, ctx.service.update_assumptions(
            ctx.params["plan_id"],
            patch,
            author=ctx.body.get("author", "advisor"),
            as_of=_parse_day(ctx.body.get("as_of")),
            change_summary=ctx.body.get("change_summary", ""),
        )

    def confirm(ctx: RequestContext) -> tuple[int, dict[str, Any]]:
        confirmed_by = ctx.body.get("confirmed_by")
        if not confirmed_by:
            raise ValueError("confirmed_by 不能为空")
        version_no = ctx.body.get("version")
        if version_no is None:
            raise ValueError("version 不能为空")
        return 200, ctx.service.confirm(
            ctx.params["plan_id"], int(version_no), confirmed_by
        )

    def stress(ctx: RequestContext) -> tuple[int, dict[str, Any]]:
        return 200, ctx.service.get_stress(ctx.params["plan_id"])

    return [
        ("GET", re.compile(r"^/health$"), health),
        ("GET", re.compile(r"^/api/market/status$"), market_status),
        ("POST", re.compile(r"^/api/plans$"), create_plan),
        ("GET", re.compile(r"^/api/plans/(?P<plan_id>[^/]+)$"), get_plan),
        ("GET", re.compile(r"^/api/plans/(?P<plan_id>[^/]+)/versions$"), list_versions),
        ("GET", re.compile(r"^/api/plans/(?P<plan_id>[^/]+)/versions/(?P<version>\d+)$"), get_version),
        ("POST", re.compile(r"^/api/plans/(?P<plan_id>[^/]+)/assumptions$"), update_assumptions),
        ("POST", re.compile(r"^/api/plans/(?P<plan_id>[^/]+)/confirm$"), confirm),
        ("GET", re.compile(r"^/api/plans/(?P<plan_id>[^/]+)/stress$"), stress),
    ]


ROUTES = _routes()


class RequestHandler(BaseHTTPRequestHandler):
    service: PlanService  # 由 create_server 注入

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    # ------------------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        for route_method, pattern, handler in ROUTES:
            if route_method != method:
                continue
            match = pattern.match(parsed.path)
            if not match:
                continue
            try:
                body = self._read_body() if method == "POST" else None
                ctx = RequestContext(self.service, query, body, match.groupdict())
                status, payload = handler(ctx)
            except (PlanNotFoundError, VersionNotFoundError) as exc:
                self._send(404, {"error": {"code": "not_found", "message": str(exc)}})
                return
            except AlreadyConfirmedError as exc:
                self._send(409, {"error": {"code": "already_confirmed", "message": str(exc)}})
                return
            except (ValueError, KeyError, TypeError) as exc:
                self._send(400, {"error": {"code": "bad_request", "message": str(exc)}})
                return
            except Exception as exc:  # noqa: BLE001 - 兜底，避免连接被直接挂断
                self._send(500, {"error": {"code": "internal", "message": str(exc)}})
                return
            self._send(status, payload)
            return
        self._send(404, {"error": {"code": "not_found", "message": "路由不存在"}})

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int, service: PlanService | None = None) -> ThreadingHTTPServer:
    handler = type("BoundRequestHandler", (RequestHandler,), {
        "service": service or PlanService(),
    })
    return ThreadingHTTPServer((host, port), handler)
