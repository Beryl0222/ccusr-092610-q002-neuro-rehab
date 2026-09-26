"""训练决策服务测试：权限、门禁、幂等、容量、恢复与患者视图。"""

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from neuro_rehab import (  # noqa: E402
    Actor,
    CapacityExceeded,
    NotFound,
    PermissionDenied,
    Role,
    ServiceError,
    StateError,
    TrainingDecisionService,
)

T0 = "2026-09-26T08:00:00+08:00"
T1 = "2026-09-26T09:00:00+08:00"
T2 = "2026-09-26T10:00:00+08:00"
T3 = "2026-09-26T11:00:00+08:00"
START = "2026-09-27T09:00:00+08:00"
END = "2026-09-27T10:00:00+08:00"

OP = Actor("op-1", frozenset({Role.OPERATOR}))
TH1 = Actor("th-1", frozenset({Role.THERAPIST}))
TH2 = Actor("th-2", frozenset({Role.THERAPIST}))
REV1 = Actor("rev-1", frozenset({Role.REVIEWER}))
REV2 = Actor("rev-2", frozenset({Role.REVIEWER}))
MT = Actor("mt-1", frozenset({Role.DEVICE_MAINTENANCE}))
PATIENT1 = Actor("patient-1", frozenset({Role.PATIENT}))


def make_service(store_path=None):
    svc = TrainingDecisionService(store_path)
    svc.register_staff(OP, "op-1", ["operator"], T0)
    svc.register_staff(OP, "th-1", ["therapist"], T0)
    svc.register_staff(OP, "th-2", ["therapist"], T0)
    svc.register_staff(OP, "rev-1", ["reviewer"], T0)
    svc.register_staff(OP, "rev-2", ["reviewer"], T0)
    svc.register_staff(OP, "mt-1", ["device_maintenance"], T0)
    svc.register_venue(OP, "venue-1", 1, T0)
    return svc


def setup_releasable(svc, patient="patient-1", booking="bk-1", start=START, end=END):
    svc.record_consent(TH1, patient, T0)
    svc.approve_device_profile(REV1, "dev-1", "revA", "dec-1.0", T0)
    svc.record_baseline(TH1, patient, "base-1", T0)
    svc.place_booking(OP, booking, patient, "th-1", "venue-1", "dev-1", start, end, T1)
    svc.record_therapist_approval(TH1, booking, T1)


class RegistrationTests(unittest.TestCase):
    def test_first_registration_requires_operator_bootstrap(self):
        svc = TrainingDecisionService()
        with self.assertRaises(PermissionDenied):
            svc.register_staff(TH1, "th-1", ["therapist"], T0)
        svc.register_staff(OP, "op-1", ["operator"], T0)

    def test_later_registration_requires_registered_operator(self):
        svc = TrainingDecisionService()
        svc.register_staff(OP, "op-1", ["operator"], T0)
        stranger = Actor("stranger", frozenset({Role.OPERATOR}))
        with self.assertRaises(PermissionDenied):
            svc.register_staff(stranger, "x", ["therapist"], T0)
        with self.assertRaises(PermissionDenied):
            svc.register_staff(TH1, "th-1", ["therapist"], T0)  # 未登记

    def test_unknown_role_rejected(self):
        svc = TrainingDecisionService()
        with self.assertRaises(ServiceError) as ctx:
            svc.register_staff(OP, "x", ["superuser"], T0)
        self.assertEqual("unknown_role", ctx.exception.code)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()

    def failed_codes(self, result):
        return {gate["code"] for gate in result["explanation"]["failed_gates"]}

    def test_full_path_releases_and_explains(self):
        setup_releasable(self.svc)
        result = self.svc.evaluate_booking(TH1, "bk-1", T2)
        self.assertEqual("released", result["decision"])
        self.assertFalse(result["deduplicated"])
        self.assertEqual([], result["explanation"]["failed_gates"])
        self.assertIn("放行", result["explanation"]["summary"])
        held = self.svc.record_session_held(TH1, "bk-1", "常规训练", T2, T2)
        self.assertEqual("SESSION_HELD", held["event_type"])

    def test_missing_device_profile_pauses(self):
        self.svc.record_consent(TH1, "patient-1", T0)
        self.svc.record_baseline(TH1, "patient-1", "base-1", T0)
        self.svc.place_booking(OP, "bk-1", "patient-1", "th-1", "venue-1", "dev-1", START, END, T1)
        self.svc.record_therapist_approval(TH1, "bk-1", T1)
        result = self.svc.evaluate_booking(TH1, "bk-1", T2)
        self.assertEqual("paused", result["decision"])
        self.assertIn("device_profile_missing", self.failed_codes(result))

    def test_device_fault_reported_by_maintenance_pauses(self):
        setup_releasable(self.svc)
        self.svc.report_device_status(MT, "dev-1", "fault", T1, note="信号异常")
        result = self.svc.evaluate_booking(TH1, "bk-1", T2)
        self.assertIn("device_unavailable", self.failed_codes(result))
        self.svc.report_device_status(MT, "dev-1", "ok", T2)
        self.assertEqual("released", self.svc.evaluate_booking(TH1, "bk-1", T3)["decision"])

    def test_missing_baseline_pauses(self):
        self.svc.record_consent(TH1, "patient-1", T0)
        self.svc.approve_device_profile(REV1, "dev-1", "revA", "dec-1.0", T0)
        self.svc.place_booking(OP, "bk-1", "patient-1", "th-1", "venue-1", "dev-1", START, END, T1)
        self.svc.record_therapist_approval(TH1, "bk-1", T1)
        result = self.svc.evaluate_booking(TH1, "bk-1", T2)
        self.assertIn("baseline_missing", self.failed_codes(result))

    def test_venue_condition_pauses(self):
        setup_releasable(self.svc)
        self.svc.record_venue_condition(OP, "venue-1", "limited", T1, note="电磁干扰")
        result = self.svc.evaluate_booking(TH1, "bk-1", T2)
        self.assertIn("venue_condition_not_ok", self.failed_codes(result))

    def test_missing_therapist_approval_pauses(self):
        self.svc.record_consent(TH1, "patient-1", T0)
        self.svc.approve_device_profile(REV1, "dev-1", "revA", "dec-1.0", T0)
        self.svc.record_baseline(TH1, "patient-1", "base-1", T0)
        self.svc.place_booking(OP, "bk-1", "patient-1", "th-1", "venue-1", "dev-1", START, END, T1)
        result = self.svc.evaluate_booking(TH1, "bk-1", T2)
        self.assertIn("therapist_approval_missing", self.failed_codes(result))

    def test_expired_consent_pauses(self):
        self.svc.record_consent(TH1, "patient-1", T0, expires_at="2026-09-26T09:30:00+08:00")
        self.svc.approve_device_profile(REV1, "dev-1", "revA", "dec-1.0", T0)
        self.svc.record_baseline(TH1, "patient-1", "base-1", T0)
        self.svc.place_booking(OP, "bk-1", "patient-1", "th-1", "venue-1", "dev-1", START, END, T1)
        self.svc.record_therapist_approval(TH1, "bk-1", T1)
        result = self.svc.evaluate_booking(TH1, "bk-1", T2)
        self.assertIn("consent_expired", self.failed_codes(result))

    def test_evaluation_is_idempotent_per_state(self):
        setup_releasable(self.svc)
        first = self.svc.evaluate_booking(TH1, "bk-1", T2)
        count = self.svc.event_count
        second = self.svc.evaluate_booking(OP, "bk-1", T3)
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(count, self.svc.event_count)
        self.svc.pause_training(TH1, "patient-1", "疲劳", T3)
        third = self.svc.evaluate_booking(TH1, "bk-1", T3)
        self.assertFalse(third["deduplicated"])
        self.assertEqual("paused", third["decision"])

    def test_naive_timestamps_rejected(self):
        setup_releasable(self.svc)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.evaluate_booking(TH1, "bk-1", "2026-09-26 10:00:00")
        self.assertEqual("invalid_timestamp", ctx.exception.code)
        with self.assertRaises(ServiceError):
            self.svc.place_booking(
                OP, "bk-2", "patient-1", "th-1", "venue-1", "dev-1",
                "2026-09-28 09:00", END, T1,
            )

    def test_session_held_requires_released_decision(self):
        setup_releasable(self.svc)
        with self.assertRaises(StateError):
            self.svc.record_session_held(TH1, "bk-1", "常规训练", T2, T2)
        self.svc.evaluate_booking(TH1, "bk-1", T2)
        self.svc.pause_training(TH1, "patient-1", "疲劳", T2)
        with self.assertRaises(StateError):
            self.svc.record_session_held(TH1, "bk-1", "常规训练", T2, T2)


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        setup_releasable(self.svc)

    def test_maintenance_can_only_report_status(self):
        report = self.svc.report_device_status(MT, "dev-1", "maintenance", T1)
        self.assertEqual("DEVICE_STATUS_REPORTED", report["event_type"])
        with self.assertRaises(PermissionDenied):
            self.svc.pause_training(MT, "patient-1", "越权", T1)
        with self.assertRaises(PermissionDenied):
            self.svc.record_therapist_approval(MT, "bk-1", T1)
        with self.assertRaises(PermissionDenied):
            self.svc.approve_device_profile(MT, "dev-1", "revB", "dec-2.0", T1)
        with self.assertRaises(PermissionDenied):
            self.svc.confirm_resume(MT, "patient-1", T1)

    def test_therapist_cannot_report_device_status_or_approve_profile(self):
        with self.assertRaises(PermissionDenied):
            self.svc.report_device_status(TH1, "dev-1", "ok", T1)
        with self.assertRaises(PermissionDenied):
            self.svc.approve_device_profile(TH1, "dev-1", "revB", "dec-2.0", T1)

    def test_resume_requires_another_qualified_person(self):
        self.svc.pause_training(TH1, "patient-1", "疲劳", T1)
        with self.assertRaises(PermissionDenied):
            self.svc.confirm_resume(TH1, "patient-1", T2)  # 同一人
        self.svc.confirm_resume(REV1, "patient-1", T2)  # 另一名有资质人员
        self.assertEqual("released", self.svc.evaluate_booking(TH1, "bk-1", T3)["decision"])

    def test_resume_without_open_pause_fails(self):
        with self.assertRaises(StateError):
            self.svc.confirm_resume(TH2, "patient-1", T1)


class ConsentWithdrawalTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        setup_releasable(self.svc)
        self.svc.evaluate_booking(TH1, "bk-1", T2)
        self.svc.record_session_held(TH1, "bk-1", "常规训练", T2, T2)
        self.svc.ingest_summary(TH1, "bk-1", "v1", {"rms": 0.5}, T2)

    def test_patient_can_withdraw_and_new_training_stops(self):
        self.svc.withdraw_consent(PATIENT1, "patient-1", T3)
        with self.assertRaises(StateError):
            self.svc.place_booking(
                OP, "bk-2", "patient-1", "th-1", "venue-1", "dev-1",
                "2026-09-28T09:00:00+08:00", "2026-09-28T10:00:00+08:00", T3,
            )
        result = self.svc.evaluate_booking(TH1, "bk-1", T3)
        self.assertEqual("paused", result["decision"])
        codes = {g["code"] for g in result["explanation"]["failed_gates"]}
        self.assertIn("consent_withdrawn", codes)

    def test_withdrawal_restricts_viewing_but_keeps_accountability(self):
        before = self.svc.case_history("patient-1")
        self.assertTrue(any(e["category"] == "clinical_detail" for e in before))
        self.svc.withdraw_consent(PATIENT1, "patient-1", T3)
        care = self.svc.case_history("patient-1", purpose="care")
        self.assertTrue(care)  # 责任记录保留
        self.assertFalse(any(e["category"] == "clinical_detail" for e in care))
        self.assertTrue(any(e["event_type"] == "SESSION_DECIDED" for e in care))
        legal = self.svc.case_history("patient-1", purpose="legal")
        self.assertTrue(any(e["category"] == "clinical_detail" for e in legal))
        self.assertGreaterEqual(self.svc.event_count, len(before))  # 记录未删除

    def test_operator_can_withdraw_on_behalf(self):
        self.svc.withdraw_consent(OP, "patient-1", T3)
        view = self.svc.patient_view("patient-1", T3)
        self.assertEqual("已撤回", view["authorization"])

    def test_double_withdrawal_fails(self):
        self.svc.withdraw_consent(PATIENT1, "patient-1", T3)
        with self.assertRaises(StateError):
            self.svc.withdraw_consent(PATIENT1, "patient-1", T3)


class VersionImmutabilityTests(unittest.TestCase):
    def test_version_update_does_not_rewrite_past_conclusions(self):
        svc = make_service()
        setup_releasable(svc)
        first = svc.evaluate_booking(TH1, "bk-1", T2)
        self.assertEqual("released", first["decision"])
        svc.approve_device_profile(REV1, "dev-1", "revB", "dec-2.0", T3)  # 版本更新
        audit = svc.decision_audit(first["event_id"])
        self.assertEqual("revA", audit["versions_at_decision"]["device_revision"])
        self.assertEqual("dec-1.0", audit["versions_at_decision"]["decoder_version"])
        self.assertEqual("revB", audit["versions_now"]["device_revision"])
        self.assertFalse(audit["conclusion_changed"])
        # 历史事件保持不变，新评估使用新版本
        self.assertEqual("released", svc.evaluate_booking(TH1, "bk-1", T3)["decision"])
        again = svc.decision_audit(first["event_id"])
        self.assertEqual("revA", again["versions_at_decision"]["device_revision"])


class SummaryIngestionTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        setup_releasable(self.svc)
        self.svc.evaluate_booking(TH1, "bk-1", T2)
        self.svc.record_session_held(TH1, "bk-1", "常规训练", T2, T2)

    def test_duplicate_summary_is_idempotent(self):
        first = self.svc.ingest_summary(TH1, "bk-1", "v1", {"rms": 0.5}, T2)
        self.assertEqual("accepted", first["status"])
        count = self.svc.event_count
        second = self.svc.ingest_summary(OP, "bk-1", "v1", {"rms": 0.5}, T3)
        self.assertEqual("duplicate", second["status"])
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(count, self.svc.event_count)

    def test_changed_summary_same_version_is_quarantined(self):
        first = self.svc.ingest_summary(TH1, "bk-1", "v1", {"rms": 0.5}, T2)
        conflict = self.svc.ingest_summary(TH1, "bk-1", "v1", {"rms": 0.9}, T3)
        self.assertEqual("quarantined", conflict["status"])
        self.assertEqual(first["fingerprint"], conflict["conflict_with"])
        quarantined = self.svc.list_quarantined()
        self.assertEqual(1, len(quarantined))
        self.assertEqual("bk-1", quarantined[0]["session_id"])
        # 原摘要仍然有效，新版本号可正常接收
        again = self.svc.ingest_summary(TH1, "bk-1", "v1", {"rms": 0.5}, T3)
        self.assertEqual("duplicate", again["status"])
        accepted = self.svc.ingest_summary(TH1, "bk-1", "v2", {"rms": 0.9}, T3)
        self.assertEqual("accepted", accepted["status"])

    def test_unknown_session_rejected(self):
        with self.assertRaises(NotFound):
            self.svc.ingest_summary(TH1, "bk-x", "v1", {"rms": 0.5}, T2)


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        self.svc.register_staff(OP, "th-9", ["therapist"], T0, max_concurrent=5)
        self.svc.register_venue(OP, "venue-9", 10, T0)

    def _book(self, booking, patient, therapist="th-9", venue="venue-9", start=START, end=END):
        self.svc.record_consent(TH1, patient, T0)
        return self.svc.place_booking(OP, booking, patient, therapist, venue, "dev-1", start, end, T1)

    def test_therapist_capacity_enforced(self):
        self._book("bk-1", "p-1", therapist="th-1")
        with self.assertRaises(CapacityExceeded) as ctx:
            self._book("bk-2", "p-2", therapist="th-1", venue="venue-9")
        self.assertEqual("capacity_exceeded", ctx.exception.code)
        # 错开时间可以预约
        self._book("bk-3", "p-3", therapist="th-1", venue="venue-9",
                   start="2026-09-27T10:00:00+08:00", end="2026-09-27T11:00:00+08:00")

    def test_venue_capacity_enforced(self):
        self._book("bk-1", "p-1", venue="venue-1")  # venue-1 容量 1
        with self.assertRaises(CapacityExceeded):
            self._book("bk-2", "p-2", venue="venue-1")

    def test_concurrent_bookings_do_not_exceed_capacity(self):
        svc = self.svc
        barrier = threading.Barrier(8)
        outcomes = []
        lock = threading.Lock()

        def attempt(index):
            patient = f"p-{index}"
            svc.record_consent(TH1, patient, T0)
            barrier.wait(timeout=10)
            try:
                svc.place_booking(OP, f"bk-c{index}", patient, "th-1", "venue-9", "dev-1", START, END, T1)
                with lock:
                    outcomes.append("ok")
            except CapacityExceeded:
                with lock:
                    outcomes.append("full")

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(1, outcomes.count("ok"))
        self.assertEqual(7, outcomes.count("full"))


class ReviewRecoveryTests(unittest.TestCase):
    def test_overdue_review_blocks_and_recovery_is_idempotent(self):
        with self.subTest("安排到期复核后评估暂停"):
            svc = make_service()
            setup_releasable(svc)
            svc.schedule_review(REV1, "patient-1", "2026-09-25T09:00:00+08:00", T0)
            result = svc.evaluate_booking(TH1, "bk-1", T2)
            self.assertEqual("paused", result["decision"])
            codes = {g["code"] for g in result["explanation"]["failed_gates"]}
            self.assertIn("review_overdue", codes)

        with self.subTest("恢复后补发到期事件且幂等"):
            emitted = svc.recover(T2)
            self.assertEqual(["review-patient-1-1"], emitted)
            self.assertEqual([], svc.recover(T3))

        with self.subTest("签署结论后放行"):
            svc.sign_review(REV1, "review-patient-1-1", "continue", T3)
            self.assertEqual("released", svc.evaluate_booking(TH1, "bk-1", T3)["decision"])

    def test_recovery_survives_restart(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            svc = make_service(path)
            setup_releasable(svc)
            svc.schedule_review(REV1, "patient-1", "2026-09-25T09:00:00+08:00", T0)
            self.assertEqual(["review-patient-1-1"], svc.recover(T2))

            restarted = TrainingDecisionService(path)  # 重放恢复
            self.assertEqual([], restarted.recover(T3))  # 不重复补发
            view = restarted.patient_view("patient-1", T3)
            self.assertEqual("待复核", view["training_status"])
            restarted.sign_review(REV1, "review-patient-1-1", "continue", T3)
            self.assertEqual("released", restarted.evaluate_booking(TH1, "bk-1", T3)["decision"])

    def test_suspend_conclusion_creates_clinical_pause(self):
        svc = make_service()
        setup_releasable(svc)
        svc.schedule_review(REV1, "patient-1", "2026-09-25T09:00:00+08:00", T0)
        svc.sign_review(REV1, "review-patient-1-1", "suspend", T2)
        view = svc.patient_view("patient-1", T2)
        self.assertEqual("已暂停", view["training_status"])
        with self.assertRaises(PermissionDenied):
            svc.confirm_resume(REV1, "patient-1", T3)  # 结论人不能自行解除
        svc.confirm_resume(REV2, "patient-1", T3)
        self.assertEqual("可训练", svc.patient_view("patient-1", T3)["training_status"])

    def test_unknown_or_signed_review_rejected(self):
        svc = make_service()
        with self.assertRaises(NotFound):
            svc.sign_review(REV1, "review-x", "continue", T2)
        svc.record_consent(TH1, "patient-1", T0)
        svc.schedule_review(REV1, "patient-1", "2026-09-30T09:00:00+08:00", T0)
        svc.sign_review(REV1, "review-patient-1-1", "continue", T2)
        with self.assertRaises(StateError):
            svc.sign_review(REV2, "review-patient-1-1", "continue", T3)


class PatientViewTests(unittest.TestCase):
    def test_view_tracks_lifecycle(self):
        svc = make_service()
        view = svc.patient_view("patient-1", T0)
        self.assertEqual("未记录", view["authorization"])
        self.assertEqual("未开始", view["training_status"])

        setup_releasable(svc)
        view = svc.patient_view("patient-1", T1)
        self.assertEqual("有效", view["authorization"])
        self.assertEqual("可训练", view["training_status"])

        svc.evaluate_booking(TH1, "bk-1", T2)
        view = svc.patient_view("patient-1", T2)
        self.assertEqual("放行", view["last_decision"]["result"])

        svc.pause_training(TH1, "patient-1", "疲劳", T2)
        view = svc.patient_view("patient-1", T2)
        self.assertEqual("已暂停", view["training_status"])
        self.assertEqual("疲劳", view["active_pause_reason"])
        self.assertTrue(view["notices"])

        svc.withdraw_consent(PATIENT1, "patient-1", T3)
        view = svc.patient_view("patient-1", T3)
        self.assertEqual("已撤回", view["authorization"])
        self.assertEqual("已停止（授权已撤回）", view["training_status"])


class ExplanationTests(unittest.TestCase):
    def test_explain_by_event_id_or_booking(self):
        svc = make_service()
        setup_releasable(svc)
        svc.pause_training(TH1, "patient-1", "疲劳", T1)
        result = svc.evaluate_booking(TH1, "bk-1", T2)
        by_event = svc.explain_decision(result["event_id"])
        by_booking = svc.explain_decision("bk-1")
        self.assertEqual(by_event["event_id"], by_booking["event_id"])
        self.assertEqual("暂停", by_event["decision_label"])
        codes = {gate["code"] for gate in by_event["failed_gates"]}
        self.assertIn("clinical_pause_active", codes)
        messages = [gate["message"] for gate in by_event["failed_gates"]]
        self.assertTrue(any("疲劳" in message for message in messages))
        with self.assertRaises(NotFound):
            svc.explain_decision("bk-x")


if __name__ == "__main__":
    unittest.main()
