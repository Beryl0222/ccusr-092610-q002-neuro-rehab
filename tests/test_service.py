import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from neuro_rehab.domain import Actor, DecisionOutcome, Role
from neuro_rehab.ledger import EventLedger
from neuro_rehab.service import (
    ALL_CHECKS_PASSED,
    SIGNAL_BELOW_BASELINE,
    SUMMARY_QUARANTINED,
    CapacityExceeded,
    Conflict,
    PermissionDenied,
    ServiceError,
    TrainingDecisionService,
)

SCHEMA = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))

T0 = "2026-09-26T09:00:00+08:00"
T1 = "2026-09-26T10:00:00+08:00"
SLOT_A = ("2026-09-27T09:00:00+08:00", "2026-09-27T10:00:00+08:00")
SLOT_B = ("2026-09-27T10:00:00+08:00", "2026-09-27T11:00:00+08:00")

THERAPIST = Actor("t-01", Role.THERAPIST)
THERAPIST_B = Actor("t-02", Role.THERAPIST)
MAINTAINER = Actor("m-01", Role.MAINTAINER)
REVIEWER = Actor("r-01", Role.REVIEWER)
AUDITOR = Actor("a-01", Role.AUDITOR)

GOOD_METRICS = {"signal_quality": 0.9, "fatigue": 0.2}
BAD_METRICS = {"signal_quality": 0.3, "fatigue": 0.2}


def build_service(ledger=None) -> TrainingDecisionService:
    service = TrainingDecisionService(ledger or EventLedger(SCHEMA))
    service.register_staff("t-01", Role.THERAPIST, T0)
    service.register_staff("t-02", Role.THERAPIST, T0)
    service.register_staff("m-01", Role.MAINTAINER, T0)
    service.register_staff("r-01", Role.REVIEWER, T0)
    service.register_staff("a-01", Role.AUDITOR, T0)
    service.register_venue("v-01", capacity=1, condition="屏蔽室", at=T0)
    return service


def ready_case(service: TrainingDecisionService, patient="p-01") -> str:
    """完成同意、设备批准、基线与开案的准备工作。"""
    service.record_consent(THERAPIST, patient, "脑控轮椅训练", T0)
    service.report_device_status(MAINTAINER, "dev-01", "rev-A", "dec-1.0", T0)
    service.approve_profile(REVIEWER, "dev-01", "rev-A", T0)
    service.set_baseline(
        THERAPIST,
        patient,
        min_signal_quality=0.6,
        max_fatigue=0.5,
        required_condition="屏蔽室",
        at=T0,
    )
    return service.open_case(THERAPIST, patient, "dev-01", T1)


class ReleaseAndExplainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()
        self.case_id = ready_case(self.service)
        self.service.book_session(THERAPIST, self.case_id, "s-01", "v-01", *SLOT_A, at=T1)

    def test_release_path_explains_versions(self) -> None:
        self.service.submit_signal_summary("s-01", "dec-1.0", GOOD_METRICS, T1)
        decision = self.service.evaluate_session(THERAPIST, "s-01", T1)
        self.assertEqual(DecisionOutcome.RELEASED, decision.outcome)
        self.assertEqual([ALL_CHECKS_PASSED], [r.code for r in decision.reasons])
        text = self.service.explain_session("s-01")
        self.assertIn("放行", text)
        self.assertIn("rev-A", text)
        self.assertIn("dec-1.0", text)
        self.assertIn("基线 v1", text)

    def test_pause_when_signal_below_baseline(self) -> None:
        self.service.submit_signal_summary("s-01", "dec-1.0", BAD_METRICS, T1)
        decision = self.service.evaluate_session(THERAPIST, "s-01", T1)
        self.assertEqual(DecisionOutcome.PAUSED, decision.outcome)
        self.assertIn(SIGNAL_BELOW_BASELINE, [r.code for r in decision.reasons])
        self.assertIn("暂停", self.service.explain_session("s-01"))

    def test_evaluation_is_idempotent(self) -> None:
        self.service.submit_signal_summary("s-01", "dec-1.0", GOOD_METRICS, T1)
        first = self.service.evaluate_session(THERAPIST, "s-01", T1)
        second = self.service.evaluate_session(THERAPIST, "s-01", T1)
        self.assertIs(first, second)


class RolePermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()
        self.case_id = ready_case(self.service)

    def test_maintainer_can_only_report_status(self) -> None:
        self.service.report_device_status(MAINTAINER, "dev-02", "rev-B", "dec-2.0", T1)
        with self.assertRaises(PermissionDenied):
            self.service.approve_profile(MAINTAINER, "dev-02", "rev-B", T1)
        with self.assertRaises(PermissionDenied):
            self.service.pause_training(MAINTAINER, self.case_id, "试图暂停", T1)
        with self.assertRaises(PermissionDenied):
            self.service.set_baseline(
                MAINTAINER, "p-01", min_signal_quality=0.5, max_fatigue=0.5, required_condition="屏蔽室", at=T1
            )

    def test_unregistered_actor_is_denied(self) -> None:
        stranger = Actor("x-09", Role.THERAPIST)
        with self.assertRaises(PermissionDenied):
            self.service.pause_training(stranger, self.case_id, "无名氏", T1)


class PauseResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()
        self.case_id = ready_case(self.service)
        self.service.pause_training(THERAPIST, self.case_id, "患者疲劳，信号波动", T1)

    def test_pause_blocks_new_booking(self) -> None:
        with self.assertRaises(Conflict):
            self.service.book_session(THERAPIST, self.case_id, "s-02", "v-01", *SLOT_A, at=T1)

    def test_resume_requires_second_qualified_person(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.service.resume_training(THERAPIST, self.case_id, "自行解除", T1)
        with self.assertRaises(PermissionDenied):
            self.service.resume_training(MAINTAINER, self.case_id, "维护人员解除", T1)
        self.service.resume_training(REVIEWER, self.case_id, "复测信号稳定，同意复训", T1)
        session_id = self.service.book_session(THERAPIST, self.case_id, "s-03", "v-01", *SLOT_A, at=T1)
        self.assertEqual("s-03", session_id)

    def test_resume_conclusion_is_traceable(self) -> None:
        self.service.resume_training(THERAPIST_B, self.case_id, "另一名治疗师确认复训", T1)
        history = self.service.case_history(THERAPIST, self.case_id)
        resumed = [e for e in history if e["event_type"] == "TRAINING_RESUMED"]
        self.assertEqual("t-02", resumed[0]["payload"]["confirmed_by"])
        self.assertEqual("另一名治疗师确认复训", resumed[0]["payload"]["conclusion"])


class VersionImmutabilityTests(unittest.TestCase):
    def test_version_update_does_not_rewrite_history(self) -> None:
        service = build_service()
        case_id = ready_case(service)
        service.book_session(THERAPIST, case_id, "s-01", "v-01", *SLOT_A, at=T1)
        service.submit_signal_summary("s-01", "dec-1.0", GOOD_METRICS, T1)
        decision = service.evaluate_session(THERAPIST, "s-01", T1)

        service.report_device_status(MAINTAINER, "dev-01", "rev-B", "dec-2.0", T1)
        service.approve_profile(REVIEWER, "dev-01", "rev-B", T1)

        kept = service.get_decision("s-01")
        self.assertEqual(decision, kept)
        self.assertEqual("rev-A", kept.device_revision)
        self.assertEqual("dec-1.0", kept.decoder_version)

        new_case = service.open_case(THERAPIST, "p-01", "dev-01", T1)
        service.book_session(THERAPIST, new_case, "s-02", "v-01", *SLOT_B, at=T1)
        service.submit_signal_summary("s-02", "dec-2.0", GOOD_METRICS, T1)
        new_decision = service.evaluate_session(THERAPIST, "s-02", T1)
        self.assertEqual("rev-B", new_decision.device_revision)
        self.assertEqual("dec-2.0", new_decision.decoder_version)


class WithdrawalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()
        self.case_id = ready_case(self.service)
        self.service.book_session(THERAPIST, self.case_id, "s-01", "v-01", *SLOT_A, at=T1)
        self.service.submit_signal_summary("s-01", "dec-1.0", GOOD_METRICS, T1)
        self.service.evaluate_session(THERAPIST, "s-01", T1)
        self.service.withdraw_consent(THERAPIST, "p-01", T1)

    def test_withdrawal_stops_new_training(self) -> None:
        with self.assertRaises(Conflict):
            self.service.book_session(THERAPIST, self.case_id, "s-02", "v-01", *SLOT_B, at=T1)
        with self.assertRaises(Conflict):
            self.service.open_case(THERAPIST, "p-01", "dev-01", T1)

    def test_patient_view_is_restricted_after_withdrawal(self) -> None:
        view = self.service.patient_view("p-01")
        self.assertEqual("withdrawn", view["consent_status"])
        self.assertEqual("stopped", view["training_status"])
        self.assertIn("依法保留", view["message"])

    def test_history_retained_but_access_restricted(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.service.case_history(THERAPIST, self.case_id)
        history = self.service.case_history(AUDITOR, self.case_id)
        self.assertIn("DECISION_RECORDED", [e["event_type"] for e in history])


class SummaryIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()
        self.case_id = ready_case(self.service)
        self.service.book_session(THERAPIST, self.case_id, "s-01", "v-01", *SLOT_A, at=T1)

    def test_duplicate_upload_deduplicates_by_fingerprint(self) -> None:
        first, dedup_first = self.service.submit_signal_summary("s-01", "dec-1.0", GOOD_METRICS, T1)
        second, dedup_second = self.service.submit_signal_summary("s-01", "dec-1.0", dict(GOOD_METRICS), T1)
        self.assertFalse(dedup_first)
        self.assertTrue(dedup_second)
        self.assertIs(first, second)
        accepted = [e for e in self.service._ledger.events() if e["event_type"] == "SUMMARY_ACCEPTED"]
        self.assertEqual(1, len(accepted))

    def test_changed_summary_same_version_is_quarantined(self) -> None:
        self.service.submit_signal_summary("s-01", "dec-1.0", GOOD_METRICS, T1)
        changed, dedup = self.service.submit_signal_summary("s-01", "dec-1.0", BAD_METRICS, T1)
        self.assertFalse(dedup)
        self.assertEqual("quarantined", changed.status)
        decision = self.service.evaluate_session(THERAPIST, "s-01", T1)
        self.assertEqual(DecisionOutcome.PAUSED, decision.outcome)
        self.assertIn(SUMMARY_QUARANTINED, [r.code for r in decision.reasons])


class CapacityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()
        self.case_id = ready_case(self.service)
        self.service.record_consent(THERAPIST, "p-02", "脑控轮椅训练", T0)
        self.service.set_baseline(
            THERAPIST, "p-02", min_signal_quality=0.6, max_fatigue=0.5, required_condition="屏蔽室", at=T0
        )
        self.case_b = self.service.open_case(THERAPIST, "p-02", "dev-01", T1)

    def test_overlapping_bookings_respect_capacity(self) -> None:
        self.service.book_session(THERAPIST, self.case_id, "s-01", "v-01", *SLOT_A, at=T1)
        with self.assertRaises(CapacityExceeded):
            self.service.book_session(THERAPIST_B, self.case_b, "s-02", "v-01", *SLOT_A, at=T1)
        with self.assertRaises(CapacityExceeded):
            self.service.book_session(THERAPIST, self.case_b, "s-03", "v-01", *SLOT_A, at=T1)
        follow_up = self.service.book_session(THERAPIST, self.case_b, "s-04", "v-01", *SLOT_B, at=T1)
        self.assertEqual("s-04", follow_up)

    def test_duplicate_booking_is_idempotent(self) -> None:
        self.service.book_session(THERAPIST, self.case_id, "s-01", "v-01", *SLOT_A, at=T1)
        again = self.service.book_session(THERAPIST, self.case_id, "s-01", "v-01", *SLOT_A, at=T1)
        self.assertEqual("s-01", again)
        booked = [e for e in self.service._ledger.events() if e["event_type"] == "SESSION_BOOKED"]
        self.assertEqual(1, len(booked))


class FollowUpRecoveryTests(unittest.TestCase):
    def test_recovery_continues_due_followups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ledger.jsonl")
            service = build_service(EventLedger(SCHEMA, path))
            case_id = ready_case(service)
            service.book_session(THERAPIST, case_id, "s-01", "v-01", *SLOT_A, at=T1)
            service.submit_signal_summary("s-01", "dec-1.0", GOOD_METRICS, T1)
            service.evaluate_session(THERAPIST, "s-01", T1)

            self.assertEqual([], service.due_followups("2026-10-02T09:00:00+08:00"))
            due_before_restart = service.due_followups("2026-10-04T09:00:00+08:00")
            self.assertEqual(["fu-s-01"], [f.followup_id for f in due_before_restart])

            restored = TrainingDecisionService.restore(SCHEMA, path)
            due_after_restart = restored.due_followups("2026-10-04T09:00:00+08:00")
            self.assertEqual(["fu-s-01"], [f.followup_id for f in due_after_restart])
            self.assertEqual("released", restored.get_decision("s-01").outcome.value)

            restored.sign_review(REVIEWER, "fu-s-01", "继续训练，维持基线", "2026-10-04T10:00:00+08:00")
            self.assertEqual([], restored.due_followups("2026-10-04T11:00:00+08:00"))


class PatientViewTests(unittest.TestCase):
    def test_simple_status_labels(self) -> None:
        service = build_service()
        unknown = service.patient_view("p-99")
        self.assertEqual("unknown", unknown["consent_status"])
        case_id = ready_case(service)
        active = service.patient_view("p-01")
        self.assertEqual("active", active["consent_status"])
        self.assertEqual("active", active["training_status"])
        service.pause_training(THERAPIST, case_id, "信号波动", T1)
        paused = service.patient_view("p-01")
        self.assertEqual("paused", paused["training_status"])
        self.assertIn("暂停", paused["message"])


class ConsentEvaluationTests(unittest.TestCase):
    def test_withdrawn_consent_pauses_evaluation_with_reason(self) -> None:
        service = build_service()
        case_id = ready_case(service)
        service.book_session(THERAPIST, case_id, "s-01", "v-01", *SLOT_A, at=T1)
        service.submit_signal_summary("s-01", "dec-1.0", GOOD_METRICS, T1)
        service.withdraw_consent(THERAPIST, "p-01", T1)
        with self.assertRaises(Conflict):
            service.evaluate_session(THERAPIST, "s-01", T1)

    def test_timezone_is_required_for_due_check(self) -> None:
        service = build_service()
        with self.assertRaises(ServiceError):
            service.due_followups("2026-10-04T09:00:00")


if __name__ == "__main__":
    unittest.main()
