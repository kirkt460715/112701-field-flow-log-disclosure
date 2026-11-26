#!/usr/bin/env python3
"""常见平台入口：``python3 app.py`` 启动本地 Web 审核台。

环境变量：
* ``HOST`` / ``AUDITDESK_HOST``：默认 127.0.0.1；
* ``PORT`` / ``AUDITDESK_PORT``：默认 8765；
* ``AUDITDESK_STATE``：运行期状态 JSON 路径。
"""
import os

from auditdesk.web import serve

if __name__ == "__main__":
    host = os.environ.get("AUDITDESK_HOST") or os.environ.get("HOST") or "127.0.0.1"
    port = int(os.environ.get("AUDITDESK_PORT") or os.environ.get("PORT") or "8765")
    raise SystemExit(serve(host=host, port=port,
                          state_path=os.environ.get("AUDITDESK_STATE")))
