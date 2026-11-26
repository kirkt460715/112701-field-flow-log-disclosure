"""零依赖本地 Web 审核台（python3 audit_console.py serve）。

只绑定本机回环地址；API 返回 JSON，页面为 auditdesk/static/index.html。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .desk import DEFAULT_STATE_PATH, AuditDesk
from .records import AuditDeskError

STATIC_INDEX = Path(__file__).parent / "static" / "index.html"


def create_server(host: str = "127.0.0.1", port: int = 8765,
                  records_dir: str | Path | None = None,
                  state_path: str | Path | None = None) -> ThreadingHTTPServer:
    desk = AuditDesk(records_dir=records_dir,
                     state_path=state_path or DEFAULT_STATE_PATH)
    lock = threading.RLock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "AuditDesk/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 保持终端安静
            return

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self) -> None:
            body = STATIC_INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise AuditDeskError(f"请求体不是合法 JSON: {exc}") from exc

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if parsed.path in ("/", "/index.html"):
                    self._send_html()
                    return
                if parsed.path == "/api/console":
                    qs = parse_qs(parsed.query)
                    at = int(qs["at"][0]) if qs.get("at") else None
                    with lock:
                        self._send_json(desk.console(at))
                    return
                if parsed.path == "/api/role":
                    qs = parse_qs(parsed.query)
                    kind = (qs.get("kind") or [""])[0]
                    at = int(qs["at"][0]) if qs.get("at") else None
                    with lock:
                        if kind == "county":
                            self._send_json(desk.county_view(qs["name"][0], at))
                        elif kind == "recipient":
                            self._send_json(desk.recipient_view(qs["group"][0], at))
                        elif kind == "audit":
                            task = {
                                "task_id": qs.get("task_id", ["web"])[0],
                                "kind": qs["task_kind"][0],
                                "scope_ids": qs.get("scope", []),
                            }
                            self._send_json(desk.audit_bundle(task, at))
                        else:
                            self._send_json({"error": "unknown role kind"}, 400)
                    return
                self._send_json({"error": "not found"}, 404)
            except (AuditDeskError, KeyError, ValueError, IndexError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                body = self._read_json_body()
                with lock:
                    if parsed.path == "/api/admit":
                        rows = body if isinstance(body, list) else body.get("records", body)
                        if isinstance(rows, dict):
                            rows = [rows]
                        admitted = desk.admit_many(rows)
                        self._send_json({"ok": True, "admitted": [r.get("id") for r in admitted]})
                        return
                    if parsed.path == "/api/ingest":
                        rows = body if isinstance(body, list) else [body]
                        saved = [desk.ingest(r) for r in rows]
                        self._send_json({"ok": True, "ingested": [r.get("id") for r in saved]})
                        return
                    if parsed.path == "/api/action":
                        result = self._handle_action(body)
                        self._send_json({"ok": True, "result": result})
                        return
                self._send_json({"ok": False, "error": "not found"}, 404)
            except (AuditDeskError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)

        def _handle_action(self, body: dict) -> dict:
            action = body.get("action")
            at = body.get("at")
            if action == "receipt":
                return desk.record_receipt(body["package"], at=at, by=body.get("by"))
            if action == "recall":
                return desk.issue_recall(body["package"], at=at, reason=body.get("reason"))
            if action == "withdraw_access":
                return desk.withdraw_access(body["fact"], at=at, recall_id=body.get("recall_id"))
            if action == "ack":
                return desk.acknowledge_recall(
                    body["recall"], at=at, outcome=body.get("outcome", "deleted"),
                    by=body.get("by"))
            if action == "access":
                return desk.record_access(
                    entry_id=body["entry"], group=body.get("group", "joint-team"),
                    purpose=body.get("purpose", "outage_review"), at=at,
                    accessor=body.get("by"))
            if action == "clock":
                return {"clock": desk.set_clock(int(body["at"]))}
            if action == "reset":
                desk.reset_runtime()
                return {"reset": True}
            raise AuditDeskError(f"未知动作: {action}")

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.desk = desk  # type: ignore[attr-defined]
    return httpd


def serve(host: str = "127.0.0.1", port: int = 8765,
          records_dir: str | Path | None = None,
          state_path: str | Path | None = None) -> int:
    httpd = create_server(host, port, records_dir, state_path)
    print(f"防御性审核台运行中: http://{host}:{port}  (Ctrl+C 停止)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        httpd.server_close()
    return 0
