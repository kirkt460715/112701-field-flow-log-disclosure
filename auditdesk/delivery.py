"""投递、收件回执与撤回回执的状态折叠和核对。

关键原则：发件端状态（sent / in_transit）不等于收件方回执。
只有拿到收件方回执，包裹才算 received；撤回必须拿到对应回执
（拦截成功或删除确认）才算 ``withdrawal_confirmed``。
"""
from __future__ import annotations

from typing import Any, Iterable

from .policy import disclose

RECEIVED_STATES = {"received", "delivered_receipt", "receipted"}
SENDER_ONLY_STATES = {"sent", "dispatched", "in_transit", "pending", "queued"}
DELETED_STATES = {"deleted", "destroyed", "deletion_confirmed", "recalled_confirmed"}
CONFIRMED_RECALL_STATES = {"acknowledged", "confirmed", "done", "completed"}

ACK_DELETION_OUTCOMES = {"deleted", "destroyed", "deletion_confirmed"}
ACK_INTERCEPT_OUTCOMES = {"intercepted", "returned", "recalled_in_transit", "withdrawn"}

STATUS_ZH = {
    "in_transit": "投递中，尚无收件方回执",
    "in_transit_recall_pending": "撤回已请求，等待撤回回执",
    "received_pending_deletion": "已经收到但待确认删除",
    "withdrawal_confirmed": "确认撤回（已取得撤回/删除回执）",
    "received_active": "对方已收到",
    "usable_summary": "可以继续使用的汇总",
}

OPEN_STATUSES = {"in_transit", "in_transit_recall_pending", "received_pending_deletion"}


# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------

def partition_records(rows: Iterable[dict]) -> dict[str, list[dict]]:
    accesses: list[dict] = []
    packages: list[dict] = []
    recalls: list[dict] = []
    acks: list[dict] = []
    receipts: list[dict] = []
    for row in rows:
        kind = row.get("kind")
        if kind == "ack" or ("outcome" in row and "for" in row and "state" not in row):
            acks.append(row)
        elif kind == "receipt" or (kind == "receipt_event"):
            receipts.append(row)
        elif "accessed_at" in row or kind == "access":
            accesses.append(row)
        elif row.get("for") and ("state" in row or "acknowledgment" in row or kind == "recall"):
            recalls.append(row)
        elif row.get("entry") or kind == "package":
            packages.append(row)
    return {
        "accesses": accesses,
        "packages": packages,
        "recalls": recalls,
        "acks": acks,
        "receipts": receipts,
    }


def _ack_confirmed(recall: dict, ack_events: list[dict]) -> dict | None:
    """返回确认信息；未确认返回 None。"""
    for event in ack_events:
        if event.get("for") == recall.get("id"):
            return {
                "at": event.get("at"),
                "outcome": event.get("outcome", "acknowledged"),
                "by": event.get("by"),
                "source": "ack_event",
                "event_id": event.get("id"),
            }
    ack = recall.get("acknowledgment")
    if ack:
        if isinstance(ack, dict):
            return {
                "at": ack.get("at", recall.get("ack_at")),
                "outcome": ack.get("outcome", recall.get("state", "acknowledged")),
                "by": ack.get("by"),
                "source": "recorded_acknowledgment",
            }
        return {
            "at": recall.get("ack_at"),
            "outcome": str(ack),
            "by": None,
            "source": "recorded_acknowledgment",
        }
    if recall.get("state") in CONFIRMED_RECALL_STATES:
        return {
            "at": recall.get("ack_at") or recall.get("acknowledged_at"),
            "outcome": recall.get("state"),
            "by": recall.get("by"),
            "source": "recall_state",
        }
    return None


def _receipt_at(pkg: dict, receipt_events: list[dict]) -> int | None:
    times: list[int] = []
    if isinstance(pkg.get("receipt_at"), int):
        times.append(pkg["receipt_at"])
    if pkg.get("state") in RECEIVED_STATES and isinstance(pkg.get("received_at"), int):
        times.append(pkg["received_at"])
    for event in receipt_events:
        if event.get("for") == pkg.get("id") and isinstance(event.get("at"), int):
            times.append(event["at"])
    return min(times) if times else None


def derive_case(
    pkg: dict,
    *,
    recalls: list[dict],
    acks: list[dict],
    receipts: list[dict],
    entries_by_id: dict[str, dict] | None = None,
) -> dict:
    entries_by_id = entries_by_id or {}
    pkg_id = pkg.get("id")
    entry = entries_by_id.get(pkg.get("entry")) if pkg.get("entry") else None
    pkg_recalls = [r for r in recalls if r.get("for") == pkg_id]
    receipt_at = _receipt_at(pkg, receipts)
    received = receipt_at is not None or pkg.get("state") in RECEIVED_STATES
    if received and receipt_at is None:
        receipt_at = pkg.get("receipt_at")

    recall_summaries = []
    confirmed: dict | None = None
    for recall in sorted(pkg_recalls, key=lambda r: r.get("requested_at", r.get("day", 0)) or 0):
        ack = _ack_confirmed(recall, acks)
        if ack is not None and confirmed is None:
            confirmed = {"recall_id": recall.get("id"), **ack}
        recall_summaries.append({
            "id": recall.get("id"),
            "state": recall.get("state", "requested"),
            "requested_at": recall.get("requested_at", recall.get("day")),
            "acknowledgment": recall.get("acknowledgment"),
            "confirmed": ack is not None,
            "confirmation": ack,
        })

    terminal_deleted = pkg.get("state") in DELETED_STATES
    is_summary = bool(entry and entry.get("granularity") == "aggregate")

    if confirmed is not None or terminal_deleted:
        status = "withdrawal_confirmed"
    elif pkg_recalls:
        status = "received_pending_deletion" if received else "in_transit_recall_pending"
    elif received:
        status = "usable_summary" if is_summary else "received_active"
    else:
        status = "in_transit"

    since = None
    if status == "in_transit":
        since = pkg.get("created") or pkg.get("sent_at")
    elif status == "in_transit_recall_pending":
        open_recall = next((r for r in recall_summaries if not r["confirmed"]), None)
        since = (open_recall or {}).get("requested_at") or pkg.get("created")
    elif status == "received_pending_deletion":
        open_recall = next((r for r in recall_summaries if not r["confirmed"]), None)
        since = (open_recall or {}).get("requested_at") or receipt_at

    next_action = {
        "in_transit": "继续跟进收件方回执，不能把发件端成功当作送达。",
        "in_transit_recall_pending": "跟进撤回回执：若在途拦截，登记 intercepted；若已送达，要求收件方删除并回执。",
        "received_pending_deletion": "包裹已被收到，必须取得删除确认回执后才能标记撤回完成。",
        "withdrawal_confirmed": "留存确认凭据，本流程关闭。",
        "received_active": "已取得收件回执；如许可收窄影响其字段，应发起撤回/删除流程。",
        "usable_summary": "汇总满足最小披露约定且无未结撤回，可继续使用。",
    }[status]

    return {
        "package_id": pkg_id,
        "entry": pkg.get("entry"),
        "origin": (entry or {}).get("origin") or pkg.get("origin"),
        "grant": pkg.get("grant"),
        "created": pkg.get("created"),
        "sender_state": pkg.get("state"),
        "recipient_receipt": receipt_at,
        "received": received,
        "is_summary": is_summary,
        "status": status,
        "status_zh": STATUS_ZH[status],
        "open": status in OPEN_STATUSES,
        "outstanding_since": since,
        "recalls": recall_summaries,
        "withdrawal_confirmation": confirmed,
        "next_action_zh": next_action,
        "raw": pkg,
    }


def build_cases(delivery_rows: Iterable[dict], entries: Iterable[dict]) -> dict[str, Any]:
    parts = partition_records(list(delivery_rows))
    entries_by_id = {e["id"]: e for e in entries}
    cases = [
        derive_case(
            pkg,
            recalls=parts["recalls"],
            acks=parts["acks"],
            receipts=parts["receipts"],
            entries_by_id=entries_by_id,
        )
        for pkg in parts["packages"]
    ]
    # 引用了不存在包裹的撤回/回执属于核对异常。
    pkg_ids = {p.get("id") for p in parts["packages"]}
    orphan_refs = []
    for recall in parts["recalls"]:
        if recall.get("for") not in pkg_ids:
            orphan_refs.append({"kind": "recall", "id": recall.get("id"), "for": recall.get("for")})
    for event in parts["receipts"] + parts["acks"]:
        target = event.get("for")
        if event in parts["receipts"] and target not in pkg_ids:
            orphan_refs.append({"kind": "receipt", "id": event.get("id"), "for": target})
        if event in parts["acks"] and not any(r.get("id") == target for r in parts["recalls"]):
            orphan_refs.append({"kind": "ack", "id": event.get("id"), "for": target})
    return {**parts, "cases": cases, "orphan_refs": orphan_refs}


# ---------------------------------------------------------------------------
# 待交日志审阅
# ---------------------------------------------------------------------------

def review_package(
    case: dict,
    *,
    entries: list[dict],
    grant_rows: list[dict],
    at: int,
    group: str = "joint-team",
    purpose: str = "outage_review",
    standing: Any = None,
) -> dict:
    """对照当前许可审阅一个尚在队列/在途的明细包裹。"""
    entry = next((e for e in entries if e["id"] == case.get("entry")), None)
    if entry is None:
        return {
            "package_id": case.get("package_id"),
            "verdict": "block",
            "reasons": [{"code": "entry_missing", "message_zh": "来源记录缺失，禁止投递。"}],
        }

    decision = disclose(
        entries=entries,
        grant_rows=grant_rows,
        entry=entry,
        group=group,
        purpose=purpose,
        at=at,
        standing=standing,
    )

    created = case.get("created")
    then_fields: list[str] = []
    if created is not None:
        then_decision = disclose(
            entries=entries,
            grant_rows=grant_rows,
            entry=entry,
            group=group,
            purpose=purpose,
            at=created,
            standing=standing,
        )
        then_fields = then_decision["permitted_fields"]

    payload_fields = list(case.get("raw", {}).get("fields") or then_fields or entry["fields_available"])
    permitted_now = set(decision["permitted_fields"])
    must_remove = [f for f in payload_fields if f not in permitted_now]

    reasons: list[dict] = list(decision["violations"])
    if must_remove and not any(v["code"] == "no_grant" for v in reasons):
        reasons.append({
            "code": "field_narrowed",
            "message_zh": f"许可已收窄，包裹中的 {must_remove} 不可再按原明细投递。",
            "fields": must_remove,
        })

    if any(v["code"] in ("no_grant", "purpose_not_authorized") for v in decision["violations"]):
        verdict = "block"
    elif any(v["code"] == "coverage_insufficient" for v in decision["violations"]):
        verdict = "hold_coarsen"
    elif must_remove:
        verdict = "redact"
    else:
        verdict = "deliver"

    return {
        "package_id": case.get("package_id"),
        "entry": entry["id"],
        "origin": entry.get("origin"),
        "at": at,
        "verdict": verdict,
        "payload_fields": payload_fields,
        "fields_at_creation": then_fields,
        "permitted_fields_now": decision["permitted_fields"],
        "must_remove_fields": must_remove,
        "releasable_fields": decision["released_fields"] if verdict == "deliver" else (
            [f for f in decision["released_fields"] if f not in must_remove]
            if verdict == "redact" else []
        ),
        "coverage": decision["coverage"],
        "coarser": decision["coarser"],
        "reasons": reasons,
        "open_withdrawal": case["status"] in ("in_transit_recall_pending", "received_pending_deletion")
        or case["status"] == "withdrawal_confirmed",
        "delivery_status": case["status"],
    }


# ---------------------------------------------------------------------------
# 核对报告
# ---------------------------------------------------------------------------

def reconcile(delivery_rows: Iterable[dict], entries: Iterable[dict], at: int) -> dict:
    model = build_cases(delivery_rows, entries)
    cases = model["cases"]
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["status"]] = counts.get(case["status"], 0) + 1

    followups = []
    for case in cases:
        if not case["open"]:
            continue
        since = case.get("outstanding_since")
        followups.append({
            "package_id": case["package_id"],
            "entry": case["entry"],
            "origin": case["origin"],
            "status": case["status"],
            "status_zh": case["status_zh"],
            "outstanding_since": since,
            "age_minutes": (at - since) if isinstance(since, int) else None,
            "next_action_zh": case["next_action_zh"],
        })
    followups.sort(key=lambda f: (-(f["age_minutes"] or -1), f["package_id"] or ""))

    return {
        "at": at,
        "status_counts": counts,
        "cases": cases,
        "open_followups": followups,
        "orphan_refs": model["orphan_refs"],
        "balanced": not followups and not model["orphan_refs"],
        "access_facts": model["accesses"],
    }
