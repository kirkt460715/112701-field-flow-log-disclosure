#!/usr/bin/env python3
"""防御性审核台的顶层入口（python3 audit_console.py ...）。

实现位于 :mod:`auditdesk` 包；此文件同时再导出常用 API，
方便以 ``from audit_console import AuditDesk`` 方式使用。
"""
from auditdesk import (  # noqa: F401
    AuditDesk,
    combined_traffic_view,
    continued_use,
    county_admin_view,
    disclose,
    joint_audit_bundle,
    reconcile,
    reassignment_effect,
    recipient_view,
    review_package,
)
from auditdesk.cli import build_parser, main  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(main())
