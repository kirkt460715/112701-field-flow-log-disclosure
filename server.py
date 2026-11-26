#!/usr/bin/env python3
"""Web 审核台入口别名：``python3 server.py``。

端口/主机/状态文件复用 app.py 的环境变量约定。
"""
import os

from auditdesk.web import serve

if __name__ == "__main__":
    host = os.environ.get("AUDITDESK_HOST") or os.environ.get("HOST") or "127.0.0.1"
    port = int(os.environ.get("AUDITDESK_PORT") or os.environ.get("PORT") or "8765")
    raise SystemExit(serve(host=host, port=port,
                          state_path=os.environ.get("AUDITDESK_STATE")))
