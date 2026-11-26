"""防御性审核台的行为验证，全部基于 records/ 与临时合成资料。"""
import io
import json
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

from auditdesk import AuditDesk
from auditdesk.cli import main as cli_main
from auditdesk.delivery import reconcile, review_package
from auditdesk.policy import (
    combined_traffic_view,
    continued_use,
    disclose,
    reassignment_effect,
    resolve_terms,
    terms_for,
)
from auditdesk.records import normalize_entries
from auditdesk.roles import joint_audit_bundle


def desk(**kwargs):
    kwargs.setdefault("persist", False)
    return AuditDesk(**kwargs)


class SeedShapeTests(unittest.TestCase):
    def setUp(self):
        self.d = desk()

    def test_three_sources_and_entries(self):
        self.assertEqual(set(self.d.sources), {"registry", "grants", "delivery"})
        ids = {e["id"] for e in self.d.entries}
        self.assertEqual(ids, {"raw-1", "raw-2", "rollup-1"})

    def test_default_clock_is_largest_recorded_time(self):
        # reassign.day=51 是种子材料中的最大时间。
        self.assertEqual(self.d.now(), 51)

    def test_granularity_classification(self):
        by_id = {e["id"]: e for e in self.d.entries}
        self.assertEqual(by_id["raw-2"]["granularity"], "device_detail")
        self.assertEqual(by_id["rollup-1"]["granularity"], "aggregate")


class GrantResolutionTests(unittest.TestCase):
    def setUp(self):
        self.d = desk()
        self.rows = self.d.grant_rows

    def test_v1_fields_before_narrowing(self):
        terms = terms_for(self.rows, "county-b", "joint-team", 44)
        self.assertIsNotNone(terms)
        self.assertEqual(set(terms.fields), {"site_token", "count"})
        self.assertEqual(terms.min_aggregate_members, 5)
        self.assertEqual(terms.purposes, ("outage_review",))

    def test_v2_narrows_fields_at_50(self):
        before = terms_for(self.rows, "county-b", "joint-team", 49)
        after = terms_for(self.rows, "county-b", "joint-team", 50)
        self.assertIn("site_token", before.fields)
        self.assertEqual(after.fields, ("count",))
        self.assertIn("grant-v2", after.revision_ids)
        # 修订未重写阈值，原最小披露约定继续有效。
        self.assertEqual(after.min_aggregate_members, 5)

    def test_data_generation_period_uses_v1(self):
        # raw-2 的产生期起点 t=20，适用 t=0 生效的 grant-v1。
        terms = terms_for(self.rows, "county-b", "joint-team", 20)
        self.assertEqual(set(terms.fields), {"site_token", "count"})

    def test_unknown_group_has_no_terms(self):
        self.assertIsNone(terms_for(self.rows, "county-b", "vendor-desk", 51))


class CoverageAndCoarserTests(unittest.TestCase):
    def setUp(self):
        self.d = desk()

    def test_raw2_is_too_sparse(self):
        decision = self.d.disclose_entry("raw-2", at=51)
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["status"], "coverage_insufficient")
        self.assertFalse(decision["coverage"]["sufficient"])
        self.assertEqual(decision["coverage"]["devices"], 2)
        self.assertEqual(decision["coverage"]["minimum"], 5)

    def test_coarser_range_is_rollup1(self):
        decision = self.d.disclose_entry("raw-2", at=51)
        coarser = decision["coarser"]
        self.assertIsNotNone(coarser)
        self.assertEqual(coarser["entry"], "rollup-1")
        self.assertEqual(coarser["minutes"], [20, 45])
        self.assertGreaterEqual(coarser["devices"], coarser["coverage"]["minimum"])
        self.assertTrue(coarser["coverage"]["sufficient"])

    def test_raw1_and_rollup1_coverages_ok(self):
        self.assertTrue(self.d.disclose_entry("raw-1", at=51)["coverage"]["sufficient"])
        self.assertTrue(self.d.disclose_entry("rollup-1", at=51)["coverage"]["sufficient"])

    def test_combined_view_applies_each_countys_minimum(self):
        view = self.d.traffic_view([20, 25], at=51)
        by_origin = {c["origin"]: c for c in view["counties"]}
        self.assertTrue(by_origin["county-a"]["disclosed"])
        self.assertEqual(by_origin["county-a"]["released_fields"], ["count"])
        b = by_origin["county-b"]
        self.assertFalse(b["disclosed"])
        self.assertEqual(b["status"], "coverage_insufficient")
        self.assertEqual(b["coarser"]["entry"], "rollup-1")
        self.assertFalse(view["merged_disclosed"])
        self.assertEqual(view["blocked_components"], ["raw-2"])


class DisclosurePurposePeriodTests(unittest.TestCase):
    def setUp(self):
        self.d = desk()

    def test_purpose_outside_duty_denied(self):
        decision = disclose(
            entries=self.d.entries, grant_rows=self.d.grant_rows,
            entry_id="raw-2", group="joint-team", purpose="marketing_reuse", at=44)
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["status"], "purpose_not_authorized")

    def test_historical_access_was_field_lawful_but_sparse(self):
        # t=44 时 grant-v1 确实给了两个字段；覆盖不足作为独立事实保留。
        decision = self.d.disclose_entry("raw-2", at=44)
        self.assertEqual(set(decision["permitted_fields"]), {"site_token", "count"})
        self.assertEqual(decision["status"], "coverage_insufficient")
        self.assertFalse(decision["coverage"]["sufficient"])

    def test_continued_use_after_narrowing(self):
        fact = next(r for r in self.d.delivery_rows if r["id"] == "view-raw")
        cu = continued_use(entries=self.d.entries, grant_rows=self.d.grant_rows,
                           access_fact=fact, at=51)
        self.assertTrue(cu["purpose_still_authorized"])
        self.assertTrue(cu["original_use_still_lawful"])
        self.assertIn("site_token", cu["fields_removed_by_narrowing"])
        self.assertEqual(cu["fields_now"], ["count"])
        # raw-2 覆盖仍不足，所以该明细区间本身现在取不到，只能走更粗汇总。
        self.assertFalse(cu["retrievable_now"])
        self.assertEqual(cu["current_decision"]["coarser"]["entry"], "rollup-1")


class ReassignmentTests(unittest.TestCase):
    def setUp(self):
        self.d = desk()

    def test_reassignment_does_not_inherit(self):
        row = next(r for r in self.d.grant_rows if r["id"] == "reassign")
        effect = reassignment_effect(self.d.grant_rows, row, at=51)
        self.assertFalse(effect["permission_inherited"])
        self.assertFalse(effect["target_grant_created"])

    def test_vendor_desk_sees_nothing(self):
        view = self.d.recipient_view("vendor-desk", 51)
        self.assertEqual(view["effective_grants"], [])
        self.assertTrue(all(not x["allowed"] for x in view["currently_disclosable"]))
        self.assertTrue(all(x["status"] == "no_grant" for x in view["currently_disclosable"]))

    def test_joint_team_keeps_surviving_count_access(self):
        view = self.d.recipient_view("joint-team", 51)
        by_entry = {x["entry"]: x for x in view["currently_disclosable"]}
        self.assertTrue(by_entry["raw-1"]["allowed"])
        self.assertIn("count", by_entry["raw-1"]["released_fields"])
        self.assertNotIn("site_token", by_entry["raw-2"]["released_fields"])
        self.assertTrue(by_entry["rollup-1"]["allowed"])
        self.assertIn("count", by_entry["rollup-1"]["released_fields"])


class DeliveryStateTests(unittest.TestCase):
    def setUp(self):
        self.d = desk()

    def test_seed_statuses_distinguished(self):
        rep = self.d.reconcile(51)
        by_id = {c["package_id"]: c for c in rep["cases"]}
        self.assertEqual(by_id["pending-2"]["status"], "in_transit_recall_pending")
        self.assertEqual(by_id["del-1"]["status"], "usable_summary")
        self.assertIsNone(by_id["pending-2"]["recipient_receipt"])
        self.assertEqual(by_id["del-1"]["recipient_receipt"], 48)
        self.assertFalse(rep["balanced"])
        self.assertEqual(rep["status_counts"].get("usable_summary"), 1)

    def test_followup_aging_uses_created_when_recall_has_no_time(self):
        rep = self.d.reconcile(51)
        item = next(f for f in rep["open_followups"] if f["package_id"] == "pending-2")
        self.assertEqual(item["outstanding_since"], 46)
        self.assertEqual(item["age_minutes"], 5)

    def test_receipt_then_deletion_confirmation_path(self):
        self.d.record_receipt("pending-2", at=52)
        case = next(c for c in self.d.reconcile(52)["cases"]
                    if c["package_id"] == "pending-2")
        self.assertEqual(case["status"], "received_pending_deletion")
        self.assertTrue(case["received"])

        self.d.acknowledge_recall("recall-2", at=53, outcome="deleted")
        rep = self.d.reconcile(53)
        case = next(c for c in rep["cases"] if c["package_id"] == "pending-2")
        self.assertEqual(case["status"], "withdrawal_confirmed")
        self.assertTrue(rep["balanced"])

    def test_intercept_ack_while_in_transit_confirms_recall(self):
        self.d.acknowledge_recall("recall-2", at=52, outcome="intercepted")
        rep = self.d.reconcile(52)
        case = next(c for c in rep["cases"] if c["package_id"] == "pending-2")
        self.assertEqual(case["status"], "withdrawal_confirmed")
        self.assertTrue(rep["balanced"])

    def test_summary_can_be_recalled_into_pending_deletion(self):
        self.d.issue_recall("del-1", at=52, recall_id="recall-del1")
        case = next(c for c in self.d.reconcile(52)["cases"] if c["package_id"] == "del-1")
        self.assertEqual(case["status"], "received_pending_deletion")
        self.d.acknowledge_recall("recall-del1", at=54, outcome="deleted")
        case = next(c for c in self.d.reconcile(54)["cases"] if c["package_id"] == "del-1")
        self.assertEqual(case["status"], "withdrawal_confirmed")
        wb = self.d.workbench(54)
        self.assertEqual(wb["active_summaries"], [])

    def test_sender_only_state_is_not_receipt(self):
        rows = [
            {"id": "p9", "entry": "raw-1", "state": "sent"},
        ]
        rep = reconcile(rows, self.d.entries, at=10)
        self.assertEqual(rep["cases"][0]["status"], "in_transit")

    def test_non_null_seed_acknowledgment_confirms(self):
        rows = [{
            "id": "p", "entry": "rollup-1", "state": "received", "receipt_at": 1,
        }, {
            "id": "r", "for": "p", "state": "requested",
            "acknowledgment": {"at": 3, "outcome": "deleted"},
        }]
        rep = reconcile(rows, self.d.entries, at=5)
        self.assertEqual(rep["cases"][0]["status"], "withdrawal_confirmed")


class ReviewQueueTests(unittest.TestCase):
    def setUp(self):
        self.d = desk()

    def test_only_unreceived_package_is_reviewed(self):
        queue = self.d.review_queue(51)["reviews"]
        ids = {r["package_id"] for r in queue}
        self.assertEqual(ids, {"pending-2"})

    def test_pending2_held_coarsen_and_site_token_removed(self):
        rv = self.d.review_queue(51)["reviews"][0]
        self.assertEqual(rv["verdict"], "hold_coarsen")
        self.assertIn("site_token", rv["must_remove_fields"])
        codes = {r["code"] for r in rv["reasons"]}
        self.assertIn("coverage_insufficient", codes)
        self.assertIn("field_narrowed", codes)
        self.assertEqual(rv["coarser"]["entry"], "rollup-1")
        self.assertTrue(rv["open_withdrawal"])

    def _case(self, package_id="p-x", entry="x", created=40, status="in_transit"):
        return {"package_id": package_id, "entry": entry, "created": created,
                "status": status, "raw": {}}

    def test_redact_verdict_for_sufficient_interval_after_narrowing(self):
        entries = normalize_entries([
            {"id": "x", "origin": "county-b", "minutes": [60, 65],
             "fields": ["site_token", "count"], "devices": 12},
        ])
        rv = review_package(self._case(entry="x"), entries=entries,
                            grant_rows=self.d.grant_rows, at=55)
        self.assertEqual(rv["verdict"], "redact")
        self.assertEqual(rv["must_remove_fields"], ["site_token"])
        self.assertEqual(rv["releasable_fields"], ["count"])

    def test_deliver_verdict_under_v1(self):
        entries = normalize_entries([
            {"id": "x", "origin": "county-b", "minutes": [40, 45],
             "fields": ["site_token", "count"], "devices": 12},
        ])
        rv = review_package(self._case(entry="x", created=40), entries=entries,
                            grant_rows=self.d.grant_rows, at=44)
        self.assertEqual(rv["verdict"], "deliver")
        self.assertEqual(set(rv["releasable_fields"]), {"site_token", "count"})

    def test_block_verdict_for_unauthorized_group(self):
        entries = normalize_entries([
            {"id": "x", "origin": "county-b", "minutes": [40, 45],
             "fields": ["site_token", "count"], "devices": 12},
        ])
        rv = review_package(self._case(entry="x"), entries=entries,
                            grant_rows=self.d.grant_rows, at=44, group="vendor-desk")
        self.assertEqual(rv["verdict"], "block")


class RoleScopingTests(unittest.TestCase):
    def setUp(self):
        self.d = desk()

    def test_county_a_sees_only_own_sources_and_no_b_cases(self):
        view = self.d.county_view("county-a", 51)
        self.assertEqual([x["entry"] for x in view["source_coverage"]], ["raw-1"])
        self.assertEqual(view["withdrawal_progress"]["cases"], [])
        self.assertTrue(view["withdrawal_progress"]["complete"])

    def test_county_b_sees_own_withdrawal_progress(self):
        view = self.d.county_view("county-b", 51)
        ids = {c["package_id"] for c in view["withdrawal_progress"]["cases"]}
        self.assertEqual(ids, {"pending-2", "del-1"})
        self.assertFalse(view["withdrawal_progress"]["complete"])

    def test_joint_audit_withdrawal_scope_minimizes_fields(self):
        bundle = self.d.audit_bundle(
            {"task_id": "t1", "kind": "withdrawal_audit", "scope_ids": ["pending-2"]}, at=51)
        self.assertTrue(bundle["authorized"])
        self.assertEqual(len(bundle["cases"]), 1)
        case = bundle["cases"][0]
        self.assertNotIn("raw", case)
        self.assertIn("status", case)
        self.assertEqual(case["package_id"], "pending-2")

    def test_joint_audit_access_task_includes_historical_decision(self):
        bundle = self.d.audit_bundle(
            {"task_id": "t2", "kind": "access_audit", "scope_ids": ["view-raw"]}, at=51)
        self.assertEqual(len(bundle["facts"]), 1)
        decision = bundle["facts"][0]["decision"]
        self.assertTrue(decision["purpose_still_authorized"])
        self.assertIn("site_token", decision["fields_removed_by_narrowing"])

    def test_joint_audit_unknown_task_refused(self):
        bundle = self.d.audit_bundle({"task_id": "x", "kind": "read_all_raw_logs"})
        self.assertFalse(bundle["authorized"])

    def test_coverage_audit_scope(self):
        bundle = self.d.audit_bundle(
            {"kind": "coverage_audit", "scope_ids": ["raw-2"]}, at=51)
        self.assertEqual([e["id"] for e in bundle["entries"]], ["raw-2"])
        self.assertEqual(bundle["entries"][0]["coarser"]["entry"], "rollup-1")


class IntakeAndPersistenceTests(unittest.TestCase):
    def test_admit_phase2_grant_and_resolve(self):
        d = desk()
        d.admit_grant({
            "id": "grant-v3", "origin": "county-a", "group": "joint-team",
            "purposes": ["outage_review"], "fields": ["count", "site_token"],
            "min_aggregate_members": 8, "effective": 60,
        })
        self.assertIsNone(terms_for(d.grant_rows, "county-a", "joint-team", 59))
        terms = terms_for(d.grant_rows, "county-a", "joint-team", 60)
        self.assertEqual(terms.min_aggregate_members, 8)

    def test_duplicate_grant_id_rejected(self):
        d = desk()
        with self.assertRaises(ValueError):
            d.admit_grant({"id": "grant-v1", "origin": "x", "group": "y"})

    def test_malformed_grant_rejected(self):
        d = desk()
        with self.assertRaises(ValueError):
            d.admit_grant({"id": "g9"})

    def test_state_persists_across_instances(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "nested" / "state.json"
            d1 = AuditDesk(state_path=state)
            d1.record_receipt("pending-2", at=52)
            self.assertTrue(state.exists())
            d2 = AuditDesk(state_path=state)
            case = next(c for c in d2.reconcile(52)["cases"]
                        if c["package_id"] == "pending-2")
            self.assertEqual(case["status"], "received_pending_deletion")

    def test_record_access_appends_audit_fact(self):
        d = desk()
        before = len(d.audit_facts)
        result = d.record_access(entry_id="raw-1", at=51)
        self.assertEqual(result["fact"]["outcome"], "fulfilled")
        self.assertEqual(len(d.audit_facts), before + 1)
        denied = d.record_access(entry_id="raw-2", group="vendor-desk", at=51)
        self.assertEqual(denied["fact"]["outcome"], "denied")

    def test_named_investigator_restriction(self):
        d = desk()
        d.admit_grant({
            "id": "grant-named", "origin": "county-a", "group": "joint-team",
            "purposes": ["outage_review"], "fields": ["site_token", "count"],
            "min_aggregate_members": 5, "effective": 0,
            "investigators": ["inspector-li"],
        })
        allowed = d.record_access(entry_id="raw-1", at=55, accessor="inspector-li")
        self.assertEqual(allowed["fact"]["outcome"], "fulfilled")
        outsider = d.record_access(entry_id="raw-1", at=55, accessor="someone-else")
        self.assertEqual(outsider["fact"]["status"], "subject_not_designated")
        anonymous = d.record_access(entry_id="raw-1", at=55)
        self.assertEqual(anonymous["fact"]["status"], "subject_not_designated")

    def test_withdraw_handed_detail_lifecycle(self):
        d = desk()
        result = d.withdraw_access("view-raw", at=52)
        package_id = result["package_id"]
        self.assertEqual(package_id, "copy-view-raw")
        case = next(c for c in d.reconcile(52)["cases"] if c["package_id"] == package_id)
        self.assertEqual(case["status"], "received_pending_deletion")
        # 幂等：再次调用不会新建第二个副本包裹。
        d.withdraw_access("view-raw", at=52)
        cases = [c for c in d.reconcile(52)["cases"] if c["package_id"] == package_id]
        self.assertEqual(len(cases), 1)
        d.acknowledge_recall(result["recall"]["id"], at=54, outcome="deleted")
        case = next(c for c in d.reconcile(54)["cases"] if c["package_id"] == package_id)
        self.assertEqual(case["status"], "withdrawal_confirmed")


class WorkbenchSeparationTests(unittest.TestCase):
    def test_three_buckets_present(self):
        wb = desk().workbench(51)
        self.assertEqual([h["fact"]["id"] for h in wb["handed_details"]], ["view-raw"])
        self.assertEqual([r["package_id"] for r in wb["pending_delivery"]], ["pending-2"])
        self.assertEqual([s["package_id"] for s in wb["active_summaries"]], ["del-1"])


class Phase2ExampleTests(unittest.TestCase):
    EX = Path(__file__).parent / "examples" / "phase2"

    def _loaded_desk(self):
        d = desk()
        d.admit_many(json.loads((self.EX / "grants-phase2.json").read_text("utf-8")))
        for row in json.loads((self.EX / "registry-phase2.json").read_text("utf-8")):
            d.ingest(row)
        for row in json.loads((self.EX / "delivery-phase2.json").read_text("utf-8")):
            d.ingest(row)
        return d

    def test_phase2_redaction_sampling_note_and_new_grant(self):
        d = self._loaded_desk()
        # county-a 的下一期许可在 t=60 后生效，阈值 8，raw-3 有 9 台。
        ok = d.disclose_entry("raw-3", at=63)
        self.assertTrue(ok["allowed"])
        self.assertIn("site_token", ok["released_fields"])
        # 采样变更说明随判定结果带出。
        raw4 = d.disclose_entry("raw-4", at=63)
        self.assertIn("采样率", raw4["sampling_note"])
        # 队列里的旧模板字段 site_token 必须在交前审阅中被要求撤下。
        review = next(r for r in d.review_queue(63)["reviews"]
                      if r["package_id"] == "pending-4")
        self.assertEqual(review["verdict"], "redact")
        self.assertEqual(review["must_remove_fields"], ["site_token"])
        self.assertEqual(review["releasable_fields"], ["count"])

    def test_phase2_full_receipt_cycle(self):
        d = self._loaded_desk()
        d.record_receipt("pending-4", at=64)
        d.issue_recall("pending-4", at=64, recall_id="recall-4")
        status = next(c["status"] for c in d.reconcile(64)["cases"]
                      if c["package_id"] == "pending-4")
        self.assertEqual(status, "received_pending_deletion")
        d.acknowledge_recall("recall-4", at=66, outcome="deleted")
        rep = d.reconcile(66)
        status = next(c["status"] for c in rep["cases"] if c["package_id"] == "pending-4")
        self.assertEqual(status, "withdrawal_confirmed")


class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(argv)
        self.assertEqual(code, 0)
        return json.loads(buf.getvalue())

    def test_overview_json(self):
        payload = self.run_cli(["--no-persist", "overview"])
        self.assertIn("reconciliation", payload)
        self.assertIn("workbench", payload)
        self.assertEqual(payload["at"], 51)

    def test_county_command(self):
        payload = self.run_cli(["--no-persist", "county", "county-a"])
        self.assertEqual(payload["county"], "county-a")
        self.assertEqual(len(payload["source_coverage"]), 1)

    def test_audit_command(self):
        payload = self.run_cli([
            "--no-persist", "audit", "--kind", "access_audit", "--scope", "view-raw"])
        self.assertTrue(payload["authorized"])
        self.assertEqual(payload["facts"][0]["id"], "view-raw")

    def test_mutation_commands_in_temp_state(self):
        with tempfile.TemporaryDirectory() as td:
            state = str(Path(td) / "state.json")
            payload = self.run_cli(["--state", state, "--at", "52", "receipt", "pending-2"])
            self.assertEqual(payload["for"], "pending-2")
            rep = self.run_cli(["--state", state, "--at", "53", "ack",
                                "recall-2", "--outcome", "deleted"])
            self.assertEqual(rep["outcome"], "deleted")
            report = self.run_cli(["--state", state, "--at", "53", "reconcile"])
            self.assertTrue(report["balanced"])


class WebApiTests(unittest.TestCase):
    def setUp(self):
        from auditdesk.web import create_server
        self.tmp = tempfile.TemporaryDirectory()
        state = str(Path(self.tmp.name) / "state.json")
        self.httpd = create_server(host="127.0.0.1", port=0, state_path=state)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def call(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.url(path), data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())

    def test_index_and_console(self):
        with urllib.request.urlopen(self.url("/"), timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("防御性审核台", resp.read().decode())
        _, body = self.call("GET", "/api/console?at=51")
        self.assertEqual(body["at"], 51)
        self.assertEqual(len(body["workbench"]["pending_delivery"]), 1)

    def test_action_flow_over_http(self):
        _, body = self.call("POST", "/api/action",
                            {"action": "receipt", "package": "pending-2", "at": 52})
        self.assertTrue(body["ok"])
        _, body = self.call("POST", "/api/action",
                            {"action": "ack", "recall": "recall-2", "outcome": "deleted", "at": 53})
        self.assertTrue(body["ok"])
        _, body = self.call("GET", "/api/console?at=53")
        self.assertTrue(body["reconciliation"]["balanced"])

    def test_admit_over_http(self):
        _, body = self.call("POST", "/api/admit", {"records": [{
            "id": "g-x", "origin": "county-a", "group": "joint-team",
            "purposes": ["outage_review"], "fields": ["count"], "effective": 70}]})
        self.assertEqual(body["admitted"], ["g-x"])

    def test_bad_action_is_400(self):
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("POST", "/api/action", {"action": "explode"})
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
