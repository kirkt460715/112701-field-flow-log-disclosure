#!/usr/bin/env bash
# 下一期（t>=60）授权、投递与核对的可重复演练。
# 只使用 examples/phase2 下的本地合成元数据；状态写入 runtime/phase2-demo-state.json。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

STATE="runtime/phase2-demo-state.json"
mkdir -p runtime
rm -f "$STATE"

PY=(python3 audit_console.py --state "$STATE")

echo "== 1) 接收下一期许可 =="
"${PY[@]}" admit examples/phase2/grants-phase2.json

echo "== 2) 接收采集部门的新区间/采样说明与投递记录 =="
"${PY[@]}" ingest examples/phase2/registry-phase2.json
"${PY[@]}" ingest examples/phase2/delivery-phase2.json

echo "== 3) t=63 审阅待交队列：pending-2 覆盖不足+字段收窄；pending-4 应 redact =="
"${PY[@]}" --at 63 queue | python3 -c '
import json,sys
d=json.load(sys.stdin)
for r in d["reviews"]:
    print(r["package_id"], r["verdict"], "remove=", r["must_remove_fields"],
          "coarser=", (r.get("coarser") or {}).get("entry"))
'

echo "== 4a) 旧包裹 pending-2 在途拦截成功 -> 确认撤回 =="
"${PY[@]}" --at 63 ack recall-2 --outcome intercepted

echo "== 4b) pending-4 先拿到收件回执 -> 已经收到但待确认删除 =="
"${PY[@]}" --at 64 receipt pending-4
"${PY[@]}" --at 64 recall pending-4 --id recall-4

echo "== 5) t=66 收到删除确认 -> 确认撤回；全部结项后核对平衡 =="
"${PY[@]}" --at 66 ack recall-4 --outcome deleted
"${PY[@]}" --at 66 reconcile | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("status_counts=", d["status_counts"], "balanced=", d["balanced"])
for f in d["open_followups"]:
    print("OPEN", f["package_id"], f["status"])
'

echo "== 6) county-b 管理员查看本县来源覆盖与撤回进度 =="
"${PY[@]}" --at 66 county county-b | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("coverage:", [(x["entry"], x["coverage"]["sufficient"]) for x in d["source_coverage"]])
print("cases:", [(c["package_id"], c["status"]) for c in d["withdrawal_progress"]["cases"]])
'
