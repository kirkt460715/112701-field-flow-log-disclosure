"""防御性审核台命令行入口。

所有命令默认输出 JSON（与 records_view 的离线读取风格一致），
``--text`` 可输出中文纯文本摘要。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .desk import DEFAULT_STATE_PATH, AuditDesk
from .policy import disclose, standing_policy
from .records import AuditDeskError


def _print(payload: Any, as_text: bool = False) -> None:
    if as_text:
        print(render_text(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def render_text(payload: Any) -> str:
    """对 overview/reconcile 等负载给出可读中文摘要；其余退化为 JSON。"""
    if not isinstance(payload, dict) or "reconciliation" not in payload:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    lines: list[str] = []
    lines.append(f"审核台时刻 t={payload.get('at')}（分钟编号）")
    rec = payload["reconciliation"]
    lines.append("投递/撤回状态计数: " + json.dumps(rec["status_counts"], ensure_ascii=False))
    for case in rec["cases"]:
        flag = "未结" if case["open"] else "关闭"
        lines.append(f"  [{flag}] {case['package_id']} ({case['entry']}, {case['origin']}) "
                     f"{case['status']} - {case['status_zh']}")
    if rec["open_followups"]:
        lines.append("待跟进:")
        for f in rec["open_followups"]:
            lines.append(f"  - {f['package_id']}: {f['next_action_zh']} "
                         f"(自 t={f['outstanding_since']}, 已 {f['age_minutes']} 分钟)")
    else:
        lines.append("无未结跟进，账实核对平衡。")
    lines.append("待交审阅:")
    for rv in payload.get("review_queue", []):
        codes = ",".join(r["code"] for r in rv["reasons"]) or "ok"
        lines.append(f"  - {rv['package_id']}: 结论={rv['verdict']} 原因=[{codes}] "
                     f"应删字段={rv['must_remove_fields']}")
    tv = payload.get("traffic_view_20_25", {})
    for c in tv.get("counties", []):
        if c["disclosed"]:
            lines.append(f"视图 {c['origin']}/{c['entry']}: 可披露字段 {c['released_fields']}")
        else:
            coarser = (c.get("coarser") or {}).get("entry")
            lines.append(f"视图 {c['origin']}/{c['entry']}: {c['status']}；"
                         f"更粗范围 -> {coarser}")
    return "\n".join(lines)


def _load_records_arg(path: str | None) -> list[dict]:
    if not path or path == "-":
        data = json.loads(sys.stdin.read())
    else:
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        if isinstance(data.get("records"), list):
            return data["records"]
        return [data]
    if not isinstance(data, list):
        raise AuditDeskError("输入需为 JSON 对象或对象数组")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audit_console", description=__doc__)
    parser.add_argument("--records", type=Path, default=None, help="合成资料目录（默认 ./records）")
    parser.add_argument("--state", type=Path, default=None, help="运行期状态文件")
    parser.add_argument("--at", type=int, default=None, help="观察时刻（分钟编号，默认取资料最大时间）")
    parser.add_argument("--no-persist", action="store_true", help="纯内存运行，不写状态文件")
    parser.add_argument("--text", action="store_true", help="输出中文文本摘要")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("overview", help="审核台完整只读模型（默认命令）")
    sub.add_parser("grants", help="列出接收的许可原文")

    p_admit = sub.add_parser("admit", help="接收新的许可/修订/转派 JSON")
    p_admit.add_argument("file", help="JSON 文件，- 表示标准输入")

    p_ingest = sub.add_parser("ingest", help="按形状接收任意下一期记录")
    p_ingest.add_argument("file")

    sub.add_parser("queue", help="审阅尚待送达的日志数据")

    p_view = sub.add_parser("view", help="两县合成流量披露视图")
    p_view.add_argument("--start", type=int, default=20)
    p_view.add_argument("--end", type=int, default=25)
    p_view.add_argument("--group", default="joint-team")
    p_view.add_argument("--purpose", default="outage_review")

    p_disclose = sub.add_parser("disclose", help="判定某条来源当前可披露内容")
    p_disclose.add_argument("entry")
    p_disclose.add_argument("--group", default="joint-team")
    p_disclose.add_argument("--purpose", default="outage_review")
    p_disclose.add_argument("--baseline", action="store_true", help="允许早期五分钟计数常态约定")

    p_access = sub.add_parser("access", help="登记一次取阅并留内部审计事实")
    p_access.add_argument("entry")
    p_access.add_argument("--group", default="joint-team")
    p_access.add_argument("--purpose", default="outage_review")
    p_access.add_argument("--by", default=None)

    p_receipt = sub.add_parser("receipt", help="登记收件方回执")
    p_receipt.add_argument("package")
    p_receipt.add_argument("--by", default=None)

    p_recall = sub.add_parser("recall", help="发起/登记撤回要求")
    p_recall.add_argument("package")
    p_recall.add_argument("--id", default=None)
    p_recall.add_argument("--reason", default=None)

    p_waccess = sub.add_parser("withdraw-access", help="对已交给排查组的明细发起删除/撤回")
    p_waccess.add_argument("fact")
    p_waccess.add_argument("--id", default=None)

    p_ack = sub.add_parser("ack", help="登记撤回/删除确认回执")
    p_ack.add_argument("recall")
    p_ack.add_argument("--outcome", default="deleted",
                       choices=["deleted", "destroyed", "intercepted", "returned",
                                "acknowledged", "withdrawn"])
    p_ack.add_argument("--by", default=None)

    sub.add_parser("reconcile", help="投递-回执-撤回核对报告")

    p_county = sub.add_parser("county", help="县级管理员视图（仅本县）")
    p_county.add_argument("name")

    p_recipient = sub.add_parser("recipient", help="收件组职责内视图")
    p_recipient.add_argument("group")

    p_audit = sub.add_parser("audit", help="联合审计按任务取最小凭据")
    p_audit.add_argument("--kind", required=True,
                         choices=["withdrawal_audit", "access_audit", "coverage_audit"])
    p_audit.add_argument("--task-id", default=None)
    p_audit.add_argument("--scope", nargs="*", default=[])

    p_serve = sub.add_parser("serve", help="启动本地 Web 审核台")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)

    p_reset = sub.add_parser("reset-runtime", help="清空运行期状态（不影响 records/）")
    p_reset.add_argument("--yes", action="store_true")
    return parser


def make_desk(args: argparse.Namespace) -> AuditDesk:
    state_path = None if args.no_persist else (args.state or DEFAULT_STATE_PATH)
    return AuditDesk(records_dir=args.records, state_path=state_path,
                     persist=not args.no_persist)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from .web import serve
            return serve(host=args.host, port=args.port, records_dir=args.records,
                         state_path=args.state)
        desk = make_desk(args)
        at = args.at

        if args.command in (None, "overview"):
            _print(desk.console(at), args.text)
        elif args.command == "grants":
            _print({"at": desk.now(at), "grants": desk.grant_rows})
        elif args.command == "admit":
            rows = _load_records_arg(args.file)
            admitted = desk.admit_many(rows)
            _print({"admitted": [r.get("id") for r in admitted]})
        elif args.command == "ingest":
            rows = _load_records_arg(args.file)
            saved = [desk.ingest(r) for r in rows]
            _print({"ingested": [r.get("id") for r in saved]})
        elif args.command == "queue":
            _print(desk.review_queue(at), args.text)
        elif args.command == "view":
            _print(desk.traffic_view([args.start, args.end], at, args.group, args.purpose))
        elif args.command == "disclose":
            _print(disclose(
                entries=desk.entries, grant_rows=desk.grant_rows, entry_id=args.entry,
                group=args.group, purpose=args.purpose, at=desk.now(at),
                standing=standing_policy() if args.baseline else None,
            ))
        elif args.command == "access":
            _print(desk.record_access(entry_id=args.entry, group=args.group,
                                      purpose=args.purpose, at=at, accessor=args.by))
        elif args.command == "receipt":
            _print(desk.record_receipt(args.package, at=at, by=args.by))
        elif args.command == "recall":
            _print(desk.issue_recall(args.package, at=at, recall_id=args.id, reason=args.reason))
        elif args.command == "withdraw-access":
            _print(desk.withdraw_access(args.fact, at=at, recall_id=args.id))
        elif args.command == "ack":
            _print(desk.acknowledge_recall(args.recall, at=at, outcome=args.outcome, by=args.by))
        elif args.command == "reconcile":
            _print(desk.reconcile(at), args.text)
        elif args.command == "county":
            _print(desk.county_view(args.name, at))
        elif args.command == "recipient":
            _print(desk.recipient_view(args.group, at))
        elif args.command == "audit":
            _print(desk.audit_bundle(
                {"task_id": args.task_id, "kind": args.kind, "scope_ids": args.scope}, at))
        elif args.command == "reset-runtime":
            if not args.yes:
                raise AuditDeskError("reset-runtime 需要 --yes 确认")
            desk.reset_runtime()
            _print({"reset": True, "state_path": str(desk.state_path)})
        else:  # pragma: no cover
            parser.error(f"未知命令: {args.command}")
        return 0
    except (AuditDeskError, ValueError, OSError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
