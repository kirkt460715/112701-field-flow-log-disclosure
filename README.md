# 跨县流量日志的共享授权与撤回核对（防御性审核台）

区间采用分钟编号。流量记录是合成元数据，不含报文内容。资料均为本地合成记录。

以 Python 3.11.2 运行，无第三方依赖。`records/` 中的 `registry`、`grants`、`delivery` 对应独立来源。每份材料是 JSON 对象数组，`id` 在同一来源内唯一。报告和观察的时间以各原始记录给出的字段为准。

```sh
python3 -m unittest -v          # 56 个测试：策略、状态机、角色、CLI、HTTP、浏览器流程的后端部分
python3 records_view.py         # 原有离线来源入口（保持不变）
python3 audit_console.py --text overview
python3 audit_console.py serve  # 本地 Web 审核台 http://127.0.0.1:8765
# 或 python3 app.py（支持 PORT / AUDITDESK_STATE 环境变量）
```

资料入口列出来源、记录标识及条数。通过 `--source` 指定来源，`--offset` 和 `--limit` 限定读取的位置与数量。`--records` 可指定同样结构的其他资料目录。

---

## 1. 种子材料讲的故事（默认观察时刻 t=51）

| 记录 | 含义 | 审核台结论 |
| --- | --- | --- |
| `grants/grant-v1` | county-b → joint-team，用途 `outage_review`，字段 `site_token,count`，最小聚合成员 5，t=0 生效 | t<50 有效 |
| `grants/grant-v2` | `for: grant-v1`，t=50 起字段收窄为 `count` | t≥50 撤下 `site_token`，阈值 5 延续 |
| `grants/reassign` | t=51 joint-team → vendor-desk 的转派 | **不继承许可**，vendor-desk 仍无任何授权 |
| `registry/raw-1` | county-a [20,25]，12 台，含 site_token | 覆盖达标；count 可按早期五分钟约定披露 |
| `registry/raw-2` | county-b [20,25]，仅 2 台 | **`coverage_insufficient` 覆盖不足**，暂缓暴露明细，更粗范围 → `rollup-1` [20,45] / 19 台 |
| `registry/rollup-1` | county-b [20,45]，19 台，聚合记录 | **可以继续使用的汇总** |
| `delivery/view-raw` | t=44 按 grant-v1 取阅 raw-2 的访问事实 | 留内部审计；原用途仍合法，收窄后字段只剩 count，但该区间覆盖仍不足，只能改取更粗汇总 |
| `delivery/pending-2` | t=46 发出、`in_transit`，且有 `recall-2 requested / acknowledgment:null` | **撤回已请求、回执未到**（`in_transit_recall_pending`），必须继续跟进 |
| `delivery/del-1` | rollup-1，t=48 取得收件回执 | `usable_summary`，可继续使用 |

## 2. 三类对象分别处理（工作台 workbench）

`AuditDesk.workbench(at)` 与 Web 页面前三个面板严格分开：

1. **`handed_details` 已经交给联合排查组的明细**：访问事实只追加、不改写；给出收窄后的续取判定（用途是否仍授权、哪些字段被撤、现在能否取阅、更粗范围）。需要时可对该已交副本发起删除/撤回（`withdraw-access`），进入标准回执状态机。
2. **`pending_delivery` 尚待送达的队列**：只含尚未取得收件回执的包裹；交前审阅结论为
   `deliver / redact / hold_coarsen / block`，并列出必须撤下的字段、覆盖判定与更粗范围。
3. **`active_summaries` 仍有效的汇总订阅**：聚合粒度、覆盖达标且无未结撤回的已收件汇总。

## 3. 关键规则与实现位置

- 许可链按 `effective` 解析：基础许可（`origin/group`）+ `for` 修订；见 `auditdesk/policy.py: resolve_terms`。
- 可披露字段同时核对 **接收组、用途、数据产生期适用版本、当前时刻版本、（可选）点名排查员**；见 `disclose()`。许可可用 `investigators: ["inspector-li"]` 或 `subjects` 点名排查员，未被点名者得到 `subject_not_designated`。
- 最小披露：每县各自的 `min_aggregate_members`；无显式许可的县回落至早期五分钟 `count` 常态约定（默认阈值 5，且只覆盖 `joint-team`，转派目标不在内）。
- 覆盖不足不报错、不暴露明细，而是返回结构化结果：
  `status="coverage_insufficient"`、`coverage{devices,minimum}`、`coarser{entry,minutes,devices,...}`。
- 两县合成视图 `combined_traffic_view([20,25])` 逐县判定，一县不足只暂缓该县分量。
- 转派 `reassignment_effect()` 恒为 `permission_inherited=false`；vendor-desk 的所有判定都是 `no_grant`。
- 投递状态机 `auditdesk/delivery.py`：
  - `in_transit`（发件端/sent 不算回执）
  - `in_transit_recall_pending`（种子 pending-2 + recall-2）
  - `received_pending_deletion`（已拿收件回执但删除未确认）
  - `withdrawal_confirmed`（拿到 `deleted/destroyed/intercepted/returned` 等回执，或非空 `acknowledgment`）
  - `usable_summary` / `received_active`
  - `reconcile()` 给出状态计数、按最久未结排序的跟进清单、账实是否平衡、孤儿引用。
- 角色最小可见：
  - 县管理员 `county_view(county)`：仅本县来源覆盖 + 本县撤回进度；
  - 收件组 `recipient_view(group)`：仅职责内判定；
  - 联合审计 `audit_bundle(task)`：任务类型只有 `withdrawal_audit / access_audit / coverage_audit`，支持 `scope_ids`，只投影该任务的凭据字段；未登记任务返回 `authorized=false`。

## 4. CLI 速览

全局参数 `--at N --state PATH --records DIR --no-persist --text`（须放在子命令前）。

```sh
python3 audit_console.py --text overview           # 中文总览
python3 audit_console.py grants                    # 许可流原文
python3 audit_console.py queue                     # 待交审阅
python3 audit_console.py view --start 20 --end 25 # 两县合成视图
python3 audit_console.py disclose raw-2            # 单条披露判定
python3 audit_console.py county county-b           # 县管理员
python3 audit_console.py recipient vendor-desk     # 转派目标（全部 no_grant）
python3 audit_console.py audit --kind access_audit --scope view-raw

# 回执链（状态持久化到 runtime/state.json，可用 --state 或 AUDITDESK_STATE 改路径）
python3 audit_console.py --at 52 receipt pending-2
python3 audit_console.py --at 52 recall pending-2
python3 audit_console.py --at 53 ack recall-2 --outcome deleted
python3 audit_console.py --at 53 withdraw-access view-raw
python3 audit_console.py --at 53 reconcile
python3 audit_console.py reset-runtime --yes
```

接收下一期材料（对象或数组 JSON；`admit` 只收许可，`ingest` 按形状自动分流
许可/新区间来源/投递/回执/访问事实）：

```sh
python3 audit_console.py admit examples/phase2/grants-phase2.json
python3 audit_console.py ingest examples/phase2/registry-phase2.json
python3 audit_console.py ingest examples/phase2/delivery-phase2.json
bash examples/phase2_demo.sh     # 可重复的完整下一期演练（隔离状态，末尾核对平衡）
```

## 5. Web 审核台与 HTTP API

```sh
python3 audit_console.py serve --host 127.0.0.1 --port 8765
# 或 PORT=8000 python3 app.py
```

页面为零依赖单页（`auditdesk/static/index.html`），包含：时刻切换、KPI、许可接收框
（可粘贴下一期许可）、三类工作台、两县视图、带操作按钮的撤回跟进、角色/审计面板。
所有数据不出本机。

| 方法/路径 | 说明 |
| --- | --- |
| `GET /api/console?at=` | 首屏完整只读模型（含 workbench/reconciliation/traffic_view） |
| `GET /api/role?kind=county&name=county-b` | 县管理员视图 |
| `GET /api/role?kind=recipient&group=vendor-desk` | 收件组视图 |
| `GET /api/role?kind=audit&task_kind=access_audit&scope=view-raw` | 任务限定的审计凭据包 |
| `POST /api/admit` | 接收许可对象或 `{"records":[...]}` |
| `POST /api/ingest` | 按形状接收任意下一期记录 |
| `POST /api/action` | `receipt / recall / ack / withdraw_access / access / clock / reset` |

## 6. 设计约定

- `records/` 种子材料始终只读；运行期许可、事件、审计事实写入独立状态文件，
  事件追加、不覆写历史；`runtime/` 已在 `.gitignore`。
- 纯标准库（`http.server`、`unittest`），无网络/外发请求。
- 常用 API：`from auditdesk import AuditDesk`；也提供同义模块 `audit_console.py`、`audit_desk.py`。
- 时间均为整数分钟；所有查询可传 `at` 做历史/未来时刻判定，缺省时钟取种子与状态中的最大时间戳（种子为 51）。
