"""HTTP 入口（标准库，零第三方依赖）。

路由：
  GET  /health
  GET  /reference
  POST /plans/seed
  GET  /plans
  GET  /plans/{plan_id}
  PUT  /plans/{plan_id}
  POST /plans/{plan_id}/rebalance?trade_date=YYYY-MM-DD
  POST /plans/{plan_id}/what-if?trade_date=...&save=true
  GET  /plans/{plan_id}/versions
  GET  /plans/{plan_id}/versions/{version_id}
  POST /plans/{plan_id}/versions/{version_id}/confirm
  GET  /plans/{plan_id}/versions/{version_id}/ancestry
"""
from __future__ import annotations

import json
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from scenarios import PatchError
from service import SERVICE

SERVICE_NAME = '家庭目标组合再平衡服务'


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "GoalRebalance/1.0"

    # ---- 基础收发 ----
    def _send_json(self, payload: dict | list, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _HttpError(400, f"请求体不是合法 JSON: {exc}")
        if not isinstance(payload, dict):
            raise _HttpError(400, "请求体必须是 JSON 对象")
        return payload

    # ---- 路由 ----
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        parts = [p for p in path.split("/") if p]
        try:
            if method == "GET" and path == "/health":
                self._send_json(health_payload())
            elif method == "GET" and path == "/reference":
                self._send_json(SERVICE.reference())
            elif method == "POST" and path == "/plans/seed":
                payload = SERVICE.ensure_seed()
                self._send_json({"saved": True, "plan_id": payload["id"]}, 201)
            elif method == "GET" and path == "/plans":
                self._send_json({"plans": SERVICE.store.list_plans()})
            elif len(parts) == 2 and parts[0] == "plans":
                pid = parts[1]
                if method == "GET":
                    self._send_json(SERVICE.store.load_plan(pid))
                if method == "PUT":
                    body = self._read_json()
                    body["id"] = pid
                    plan = SERVICE.save_plan_payload(body)
                    self._send_json({"saved": True, "plan": plan.to_dict()})
            elif len(parts) == 3 and parts[0] == "plans" \
                    and parts[2] == "rebalance" and method == "POST":
                self._send_json(
                    SERVICE.rebalance(parts[1], query.get("trade_date")), 201)
            elif len(parts) == 3 and parts[0] == "plans" \
                    and parts[2] == "what-if" and method == "POST":
                body = self._read_json()
                result = SERVICE.what_if(
                    parts[1], body, query.get("trade_date"),
                    save=query.get("save") == "true",
                    label=query.get("label", "what-if"))
                self._send_json(result, 201)
            elif len(parts) == 3 and parts[0] == "plans" \
                    and parts[2] == "versions" and method == "GET":
                self._send_json(SERVICE.versions(parts[1]))
            elif len(parts) == 4 and parts[0] == "plans" \
                    and parts[2] == "versions":
                pid, vid = parts[1], parts[3]
                if method == "GET":
                    self._send_json(SERVICE.version(pid, vid))
            elif len(parts) == 5 and parts[0] == "plans" \
                    and parts[2] == "versions" and parts[4] == "confirm":
                if method == "POST":
                    self._send_json(
                        SERVICE.confirm(parts[1], parts[3],
                                        self._read_json()), 200)
            elif len(parts) == 5 and parts[0] == "plans" \
                    and parts[2] == "versions" and parts[4] == "ancestry":
                if method == "GET":
                    self._send_json(SERVICE.ancestry(parts[1], parts[3]))
            else:
                raise _HttpError(404, f"未找到路由: {method} {path}")
        except _HttpError as exc:
            self._send_json({"error": exc.message}, exc.status)
        except KeyError as exc:
            self._send_json({"error": str(exc).strip("'")}, 404)
        except (ValueError, PatchError) as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # pragma: no cover - 兜底
            traceback.print_exc()
            self._send_json({"error": f"服务器内部错误: {exc}"}, 500)

    def log_message(self, format: str, *args: object) -> None:
        return


class _HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), RequestHandler)
