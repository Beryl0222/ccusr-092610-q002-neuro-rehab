"""训练决策服务：把同意、设备版本、基线、场地、批准、信号摘要、
暂停原因与复训结论连成可追溯记录。

服务只管理评估与授权，不接入硬件，也不替代医疗判断。
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from .domain import Actor, Decision, DecisionOutcome, Reason, Role
from .ledger import EventLedger

# 评估检查不通过时使用的理由代码
CONSENT_MISSING = "CONSENT_MISSING"
CONSENT_WITHDRAWN = "CONSENT_WITHDRAWN"
CLINICAL_PAUSE_ACTIVE = "CLINICAL_PAUSE_ACTIVE"
PROFILE_NOT_APPROVED = "PROFILE_NOT_APPROVED"
SUMMARY_MISSING = "SUMMARY_MISSING"
SUMMARY_QUARANTINED = "SUMMARY_QUARANTINED"
SIGNAL_BELOW_BASELINE = "SIGNAL_BELOW_BASELINE"
FATIGUE_ABOVE_LIMIT = "FATIGUE_ABOVE_LIMIT"
VENUE_CONDITION_MISMATCH = "VENUE_CONDITION_MISMATCH"
ALL_CHECKS_PASSED = "ALL_CHECKS_PASSED"

_QUALIFIED_ROLES = (Role.THERAPIST, Role.REVIEWER)


class ServiceError(Exception):
    """业务规则拒绝；code 稳定，message 为中文说明。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PermissionDenied(ServiceError):
    def __init__(self, message: str) -> None:
        super().__init__("permission_denied", message)


class NotFound(ServiceError):
    def __init__(self, message: str) -> None:
        super().__init__("not_found", message)


class Conflict(ServiceError):
    def __init__(self, message: str) -> None:
        super().__init__("conflict", message)


class CapacityExceeded(ServiceError):
    def __init__(self, message: str) -> None:
        super().__init__("capacity_exceeded", message)


def _parse_moment(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ServiceError("timezone_required", "时间必须携带时区")
    return parsed


def _fingerprint(session_id: str, decoder_version: str, metrics: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {"session_id": session_id, "decoder_version": decoder_version, "metrics": metrics},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class _Staff:
    actor_id: str
    role: Role
    max_concurrent: int


@dataclass
class _Venue:
    venue_id: str
    capacity: int
    condition: str


@dataclass
class _Consent:
    patient_id: str
    scope: str
    status: str  # active / withdrawn
    recorded_at: str
    withdrawn_at: str | None = None


@dataclass
class _Profile:
    device_id: str
    revision: str
    decoder_version: str
    status: str  # reported / approved
    reported_by: str
    approved_by: str | None = None
    approved_at: str | None = None


@dataclass
class _Baseline:
    patient_id: str
    version: int
    min_signal_quality: float
    max_fatigue: float
    required_condition: str
    set_by: str
    set_at: str


@dataclass
class _Case:
    case_id: str
    patient_id: str
    device_id: str
    device_revision: str
    decoder_version: str
    baseline_version: int
    status: str  # active / paused
    paused_by: str | None = None
    pause_reason: str | None = None
    resumed_by: str | None = None
    resume_conclusion: str | None = None


@dataclass
class _Session:
    session_id: str
    case_id: str
    therapist_id: str
    venue_id: str
    start: str
    end: str
    status: str  # booked / released / paused / cancelled


@dataclass
class _Summary:
    session_id: str
    decoder_version: str
    fingerprint: str
    metrics: Mapping[str, Any]
    status: str  # accepted / quarantined
    uploaded_at: str


@dataclass
class _FollowUp:
    followup_id: str
    case_id: str
    session_id: str
    due_at: str
    status: str  # pending / signed
    signed_by: str | None = None
    decision: str | None = None


class TrainingDecisionService:
    """训练评估与授权服务。

    所有状态变更先写入事件账再应用到内存状态；
    用 restore 从账本文件恢复后，到期复核等职责继续履行。
    """

    def __init__(
        self,
        ledger: EventLedger,
        *,
        followup_interval: timedelta = timedelta(days=7),
    ) -> None:
        self._ledger = ledger
        self._followup_interval = followup_interval
        self._lock = threading.RLock()
        self._staff: dict[str, _Staff] = {}
        self._venues: dict[str, _Venue] = {}
        self._consents: dict[str, _Consent] = {}
        self._profiles: dict[tuple[str, str], _Profile] = {}
        self._baselines: dict[str, list[_Baseline]] = {}
        self._cases: dict[str, _Case] = {}
        self._sessions: dict[str, _Session] = {}
        self._summaries: dict[tuple[str, str], _Summary] = {}
        self._quarantined: dict[tuple[str, str], _Summary] = {}
        self._decisions: dict[str, Decision] = {}
        self._followups: dict[str, _FollowUp] = {}
        self._versions: dict[tuple[str, str], int] = {}

    @classmethod
    def restore(
        cls,
        schema: Mapping[str, Any],
        ledger_path: str,
        *,
        followup_interval: timedelta = timedelta(days=7),
    ) -> "TrainingDecisionService":
        """从账本文件恢复服务，恢复后继续履行到期复核等职责。"""
        ledger = EventLedger.load(schema, ledger_path)
        service = cls(ledger, followup_interval=followup_interval)
        for event in ledger.events():
            service._apply(event)
        return service

    # ------------------------------------------------------------------
    # 事件写入与状态推进
    # ------------------------------------------------------------------

    def _commit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: str,
        payload: Mapping[str, Any],
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            key = (aggregate_type, aggregate_id)
            version = self._versions.get(key, 0) + 1
            event = {
                "event_id": event_id or f"{aggregate_id}-{version}-{uuid.uuid4().hex[:8]}",
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "occurred_at": occurred_at,
                "version": version,
                "payload": dict(payload),
            }
            if self._ledger.append(event):
                self._apply(event)
            return event

    def _apply(self, event: Mapping[str, Any]) -> None:
        key = (event["aggregate_type"], event["aggregate_id"])
        self._versions[key] = max(self._versions.get(key, 0), event["version"])
        handler = getattr(self, f"_on_{event['event_type'].lower()}", None)
        if handler is not None:
            handler(event["aggregate_id"], event["payload"], event["occurred_at"])

    def _staff_of(self, actor: Actor) -> _Staff:
        staff = self._staff.get(actor.actor_id)
        if staff is None or staff.role != actor.role:
            raise PermissionDenied("操作人未在人员名册登记或角色不符")
        return staff

    def _require(self, actor: Actor, roles: tuple[Role, ...], action: str) -> _Staff:
        staff = self._staff_of(actor)
        if staff.role not in roles:
            raise PermissionDenied(f"{action}需要登记在册的相应角色")
        return staff

    # ------------------------------------------------------------------
    # 登记：人员、场地、同意
    # ------------------------------------------------------------------

    def register_staff(self, actor_id: str, role: Role, at: str, *, max_concurrent: int = 1) -> None:
        """登记人员角色；重复登记相同信息幂等。"""
        existing = self._staff.get(actor_id)
        if existing is not None:
            if existing.role == role and existing.max_concurrent == max_concurrent:
                return
            raise Conflict("人员已登记且角色或容量不同")
        self._commit(
            "STAFF_REGISTERED",
            "staff_roster",
            actor_id,
            at,
            {"role": role.value, "max_concurrent": max_concurrent},
        )

    def _on_staff_registered(self, aggregate_id: str, payload: Mapping[str, Any], at: str) -> None:
        self._staff[aggregate_id] = _Staff(aggregate_id, Role(payload["role"]), int(payload.get("max_concurrent", 1)))

    def register_venue(self, venue_id: str, capacity: int, condition: str, at: str) -> None:
        """登记场地容量与条件；重复登记相同信息幂等。"""
        existing = self._venues.get(venue_id)
        if existing is not None:
            if existing.capacity == capacity and existing.condition == condition:
                return
            raise Conflict("场地已登记且容量或条件不同")
        self._commit(
            "VENUE_REGISTERED",
            "venue_profile",
            venue_id,
            at,
            {"capacity": capacity, "condition": condition},
        )

    def _on_venue_registered(self, aggregate_id: str, payload: Mapping[str, Any], at: str) -> None:
        self._venues[aggregate_id] = _Venue(aggregate_id, int(payload["capacity"]), str(payload["condition"]))

    def record_consent(self, actor: Actor, patient_id: str, scope: str, at: str) -> None:
        """记录患者同意；同一患者重复记录相同范围幂等。"""
        self._require(actor, _QUALIFIED_ROLES, "记录患者同意")
        existing = self._consents.get(patient_id)
        if existing is not None and existing.status == "active" and existing.scope == scope:
            return
        self._commit(
            "CONSENT_RECORDED",
            "patient_consent",
            patient_id,
            at,
            {"scope": scope, "recorded_by": actor.actor_id},
        )

    def _on_consent_recorded(self, aggregate_id: str, payload: Mapping[str, Any], at: str) -> None:
        self._consents[aggregate_id] = _Consent(aggregate_id, str(payload.get("scope", "")), "active", at)

    def withdraw_consent(self, actor: Actor, patient_id: str, at: str) -> None:
        """患者撤回授权：停止新训练，未开始的预约取消，既有记录依法保留。"""
        self._staff_of(actor)
        consent = self._consents.get(patient_id)
        if consent is None:
            raise NotFound("未找到该患者的同意记录")
        if consent.status == "withdrawn":
            return
        self._commit(
            "CONSENT_WITHDRAWN",
            "patient_consent",
            patient_id,
            at,
            {"requested_by": actor.actor_id},
        )

    def _on_consent_withdrawn(self, aggregate_id: str, payload: Mapping[str, Any], at: str) -> None:
        consent = self._consents[aggregate_id]
        consent.status = "withdrawn"
        consent.withdrawn_at = at
        for case in self._cases.values():
            if case.patient_id != aggregate_id:
                continue
            for session in self._sessions.values():
                if session.case_id == case.case_id and session.status == "booked":
                    session.status = "cancelled"

    # ------------------------------------------------------------------
    # 设备档案与个体基线
    # ------------------------------------------------------------------

    def report_device_status(self, actor: Actor, device_id: str, revision: str, decoder_version: str, at: str) -> None:
        """设备维护人员上报设备与解码器版本状态；仅限上报，不含批准。"""
        self._require(actor, (Role.MAINTAINER,), "上报设备状态")
        if (device_id, revision) in self._profiles:
            return
        self._commit(
            "DEVICE_STATUS_REPORTED",
            "device_profile",
            device_id,
            at,
            {"device_revision": revision, "decoder_version": decoder_version, "reporter_id": actor.actor_id},
        )

    def _on_device_status_reported(self, aggregate_id: str, payload: Mapping[str, Any], at: str) -> None:
        revision = str(payload["device_revision"])
        self._profiles[(aggregate_id, revision)] = _Profile(
            aggregate_id,
            revision,
            str(payload["decoder_version"]),
            "reported",
            str(payload["reporter_id"]),
        )

    def approve_profile(self, actor: Actor, device_id: str, revision: str, at: str) -> None:
        """有资质复核人员批准设备档案版本。"""
        self._require(actor, (Role.REVIEWER,), "批准设备档案")
        profile = self._profiles.get((device_id, revision))
        if profile is None:
            raise NotFound("设备档案版本未上报")
        if profile.status == "approved":
            return
        self._commit(
            "PROFILE_APPROVED",
            "device_profile",
            device_id,
            at,
            {"device_revision": revision, "reviewer_id": actor.actor_id},
        )

    def _on_profile_approved(self, aggregate_id: str, payload: Mapping[str, Any], at: str) -> None:
        profile = self._profiles[(aggregate_id, str(payload["device_revision"]))]
        profile.status = "approved"
        profile.approved_by = str(payload["reviewer_id"])
        profile.approved_at = at

    def set_baseline(
        self,
        actor: Actor,
        patient_id: str,
        *,
        min_signal_quality: float,
        max_fatigue: float,
        required_condition: str,
        at: str,
    ) -> int:
        """治疗师设定个体训练基线，返回递增的基线版本号。"""
        self._require(actor, (Role.THERAPIST,), "设定训练基线")
        consent = self._consents.get(patient_id)
        if consent is None or consent.status != "active":
            raise Conflict("设定基线需要有效的患者同意")
        version = len(self._baselines.get(patient_id, [])) + 1
        self._commit(
            "BASELINE_SET",
            "training_baseline",
            patient_id,
            at,
            {
                "baseline_version": version,
                "therapist_id": actor.actor_id,
                "min_signal_quality": min_signal_quality,
                "max_fatigue": max_fatigue,
                "required_condition": required_condition,
            },
        )
        return version

    def _on_baseline_set(self, aggregate_id: str, payload: Mapping[str, Any], at: str) -> None:
        baseline = _Baseline(
            aggregate_id,
            int(payload["baseline_version"]),
            float(payload["min_signal_quality"]),
            float(payload["max_fatigue"]),
            str(payload["required_condition"]),
            str(payload["therapist_id"]),
            at,
        )
        self._baselines.setdefault(aggregate_id, []).append(baseline)

    def _latest_baseline(self, patient_id: str) -> _Baseline | None:
        history = self._baselines.get(patient_id)
        return history[-1] if history else None

    def _latest_approved_profile(self, device_id: str) -> _Profile | None:
        approved = [
            profile
            for (profile_device, _), profile in self._profiles.items()
            if profile_device == device_id and profile.status == "approved"
        ]
        if not approved:
            return None
        return max(approved, key=lambda profile: profile.approved_at or "")

    # ------------------------------------------------------------------
    # 训练案例与预约
    # ------------------------------------------------------------------

    def open_case(self, actor: Actor, patient_id: str, device_id: str, at: str, *, case_id: str | None = None) -> str:
        """开立训练案例，固化当前获批的设备版本与最新基线版本。"""
        self._require(actor, (Role.THERAPIST,), "开立训练案例")
        consent = self._consents.get(patient_id)
        if consent is None or consent.status != "active":
            raise Conflict("开立训练案例需要有效的患者同意")
        profile = self._latest_approved_profile(device_id)
        if profile is None:
            raise Conflict("设备档案版本尚未获批")
        baseline = self._latest_baseline(patient_id)
        if baseline is None:
            raise Conflict("尚未设定个体训练基线")
        case_id = case_id or f"case-{patient_id}-{len(self._cases) + 1}"
        if case_id in self._cases:
            return case_id
        self._commit(
            "CASE_OPENED",
            "training_case",
            case_id,
            at,
            {
                "patient_id": patient_id,
                "device_id": device_id,
                "device_revision": profile.revision,
                "decoder_version": profile.decoder_version,
                "baseline_version": baseline.version,
            },
        )
        return case_id

    def _on_case_opened(self, aggregate_id: str, payload: Mapping[str, Any], at: str) -> None:
        self._cases[aggregate_id] = _Case(
            aggregate_id,
            str(payload["patient_id"]),
            str(payload["device_id"]),
            str(payload["device_revision"]),
            str(payload["decoder_version"]),
            int(payload["baseline_version"]),
            "active",
        )

    def book_session(
        self,
        actor: Actor,
        case_id: str,
        session_id: str,
        venue_id: str,
        start: str,
        end: str,
        at: str,
    ) -> str:
        """治疗师预约训练；并发预约不得超过治疗师与场地容量。"""
        staff = self._require(actor, (Role.THERAPIST,), "预约训练")
        existing = self._sessions.get(session_id)
        if existing is not None:
            return session_id
        case = self._cases.get(case_id)
        if case is None:
            raise NotFound("训练案例不存在")
        if case.status == "paused":
            raise Conflict("临床暂停未解除，不能预约新训练")
        consent = self._consents.get(case.patient_id)
        if consent is None or consent.status != "active":
            raise Conflict("患者同意无效或已撤回，不能预约新训练")
        venue = self._venues.get(venue_id)
        if venue is None:
            raise NotFound("场地未登记")
        start_at, end_at = _parse_moment(start), _parse_moment(end)
        if end_at <= start_at:
            raise ServiceError("invalid_slot", "预约结束时间必须晚于开始时间")
        with self._lock:
            self._check_capacity(staff, venue, start_at, end_at)
            self._commit(
                "SESSION_BOOKED",
                "training_session",
                session_id,
                at,
                {
                    "case_id": case_id,
                    "therapist_id": actor.actor_id,
                    "venue_id": venue_id,
                    "start": start,
                    "end": end,
                },
            )
        return session_id

    def _check_capacity(self, staff: _Staff, venue: _Venue, start: datetime, end: datetime) -> None:
        def overlaps(session: _Session) -> bool:
            if session.status not in ("booked", "released"):
                return False
            other_start, other_end = _parse_moment(session.start), _parse_moment(session.end)
            return start < other_end and other_start < end

        therapist_busy = sum(1 for s in self._sessions.values() if s.therapist_id == staff.actor_id and overlaps(s))
        if therapist_busy >= staff.max_concurrent:
            raise CapacityExceeded("该治疗师同时段预约已满")
        venue_busy = sum(1 for s in self._sessions.values() if s.venue_id == venue.venue_id and overlaps(s))
        if venue_busy >= venue.capacity:
            raise CapacityExceeded("该场地同时段预约已满")

    def _on_session_booked(self, aggregate_id: str, payload: Mapping[str, Any], at: str) -> None:
        self._sessions[aggregate_id] = _Session(
            aggregate_id,
            str(payload["case_id"]),
            str(payload["therapist_id"]),
            str(payload["venue_id"]),
            str(payload["start"]),
            str(payload["end"]),
            "booked",
        )

    # ------------------------------------------------------------------
    # 信号质量摘要：指纹幂等，版本相同但内容变化要隔离
    # ------------------------------------------------------------------

    def submit_signal_summary(
        self,
        session_id: str,
        decoder_version: str,
        metrics: Mapping[str, Any],
        uploaded_at: str,
    ) -> tuple[_Summary, bool]:
        """上传信号质量摘要，返回（摘要， 是否重复去重）。

        相同内容按指纹幂等；同一训练同一解码器版本内容变化时，
        新摘要被隔离，既有已接受摘要保留待查；
        冲突澄清前该版本摘要不参与评估。
        """
        if session_id not in self._sessions:
            raise NotFound("训练预约不存在")
        fingerprint = _fingerprint(session_id, decoder_version, metrics)
        key = (session_id, decoder_version)
        accepted = self._summaries.get(key)
        if accepted is not None:
            if accepted.fingerprint == fingerprint:
                return accepted, True
            quarantined = self._quarantined.get(key)
            if quarantined is not None and quarantined.fingerprint == fingerprint:
                return quarantined, True
            event_id = f"summary-quar-{session_id}-{decoder_version}-{fingerprint[:12]}"
            self._commit(
                "SUMMARY_QUARANTINED",
                "training_session",
                session_id,
                uploaded_at,
                {
                    "session_id": session_id,
                    "decoder_version": decoder_version,
                    "fingerprint": fingerprint,
                    "reason": "同一解码器版本的摘要内容发生变化，已隔离待查",
                    "metrics": dict(metrics),
                },
                event_id=event_id,
            )
            return self._quarantined[key], False
        event_id = f"summary-{session_id}-{decoder_version}-{fingerprint[:12]}"
        self._commit(
            "SUMMARY_ACCEPTED",
            "training_session",
            session_id,
            uploaded_at,
            {
                "session_id": session_id,
                "decoder_version": decoder_version,
                "fingerprint": fingerprint,
                "metrics": dict(metrics),
            },
            event_id=event_id,
        )
        return self._summaries[key], False

    def _on_summary_accepted(self, aggregate_id: str, payload: Mapping[str, Any], at: str) -> None:
        key = (str(payload["session_id"]), str(payload["decoder_version"]))
        self._summaries[key] = _Summary(
            key[0], key[1], str(payload["fingerprint"]), payload.get("metrics", {}), "accepted", at
        )

    def _on_summary_quarantined(self, aggregate_id: str, payload: Mapping[str, Any], at: str) -> None:
        key = (str(payload["session_id"]), str(payload["decoder_version"]))
        self._quarantined[key] = _Summary(
            key[0], key[1], str(payload["fingerprint"]), payload.get("metrics", {}), "quarantined", at
        )

    # ------------------------------------------------------------------
    # 评估：放行或暂停，并说明原因
    # ------------------------------------------------------------------

    def evaluate_session(self, actor: Actor, session_id: str, at: str) -> Decision:
        """评估某次训练，给出放行或暂停结论及理由；重复评估幂等。"""
        self._require(actor, (Role.THERAPIST,), "评估训练")
        existing = self._decisions.get(session_id)
        if existing is not None:
            return existing
        session = self._sessions.get(session_id)
        if session is None:
            raise NotFound("训练预约不存在")
        if session.status == "cancelled":
            raise Conflict("预约已取消，不能评估")
        case = self._cases[session.case_id]
        baseline = self._baselines[case.patient_id][case.baseline_version - 1]
        reasons: list[Reason] = []

        consent = self._consents.get(case.patient_id)
        if consent is None:
            reasons.append(Reason(CONSENT_MISSING, "缺少患者同意记录"))
        elif consent.status == "withdrawn":
            reasons.append(Reason(CONSENT_WITHDRAWN, "患者已撤回授权"))
        if case.status == "paused":
            reasons.append(Reason(CLINICAL_PAUSE_ACTIVE, f"临床暂停未解除：{case.pause_reason}"))
        profile = self._profiles.get((case.device_id, case.device_revision))
        if profile is None or profile.status != "approved":
            reasons.append(Reason(PROFILE_NOT_APPROVED, "案例固化的设备档案版本未获批"))

        key = (session_id, case.decoder_version)
        summary = self._summaries.get(key)
        if key in self._quarantined:
            reasons.append(Reason(SUMMARY_QUARANTINED, "同一解码器版本的信号摘要内容冲突，已隔离待查"))
        elif summary is None:
            reasons.append(Reason(SUMMARY_MISSING, "缺少信号质量摘要"))
        else:
            metrics = summary.metrics
            if float(metrics.get("signal_quality", 0.0)) < baseline.min_signal_quality:
                reasons.append(Reason(SIGNAL_BELOW_BASELINE, "信号质量低于个体基线"))
            if float(metrics.get("fatigue", 1.0)) > baseline.max_fatigue:
                reasons.append(Reason(FATIGUE_ABOVE_LIMIT, "疲劳指标超过个体基线"))
        venue = self._venues.get(session.venue_id)
        if venue is None or venue.condition != baseline.required_condition:
            reasons.append(Reason(VENUE_CONDITION_MISMATCH, "场地条件不满足基线要求"))

        if reasons:
            outcome = DecisionOutcome.PAUSED
        else:
            outcome = DecisionOutcome.RELEASED
            reasons.append(Reason(ALL_CHECKS_PASSED, "同意、版本、基线、场地与信号检查全部通过"))
        self._commit(
            "DECISION_RECORDED",
            "training_session",
            session_id,
            at,
            {
                "session_id": session_id,
                "case_id": case.case_id,
                "decision": outcome.value,
                "reasons": [{"code": r.code, "message": r.message} for r in reasons],
                "evaluator_id": actor.actor_id,
                "device_revision": case.device_revision,
                "decoder_version": case.decoder_version,
                "baseline_version": case.baseline_version,
            },
        )
        decision = self._decisions[session_id]
        if outcome is DecisionOutcome.RELEASED:
            followup_id = f"fu-{session_id}"
            due_at = (_parse_moment(at) + self._followup_interval).isoformat()
            self._commit(
                "FOLLOWUP_DUE",
                "safety_review",
                followup_id,
                at,
                {"case_id": case.case_id, "session_id": session_id, "due_at": due_at},
                event_id=followup_id,
            )
        return decision

    def _on_decision_recorded(self, aggregate_id: str, payload: Mapping[str, Any], at: str) -> None:
        session_id = str(payload["session_id"])
        self._decisions[session_id] = Decision(
            session_id,
            DecisionOutcome(payload["decision"]),
            tuple(Reason(str(r["code"]), str(r["message"])) for r in payload.get("reasons", [])),
            str(payload["device_revision"]),
            str(payload["decoder_version"]),
            int(payload["baseline_version"]),
            str(payload["evaluator_id"]),
            at,
        )
        session = self._sessions.get(session_id)
        if session is not None and session.status == "booked":
            session.status = str(payload["decision"])

    def _on_followup_due(self, aggregate_id: str, payload: Mapping[str, Any], at: str) -> None:
        self._followups[aggregate_id] = _FollowUp(
            aggregate_id,
            str(payload["case_id"]),
            str(payload["session_id"]),
            str(payload["due_at"]),
            "pending",
        )

    # ------------------------------------------------------------------
    # 临床暂停与复训：解除暂停须由另一名有资质人员确认
    # ------------------------------------------------------------------

    def pause_training(self, actor: Actor, case_id: str, reason: str, at: str) -> None:
        """治疗师暂停训练，记录暂停原因。"""
        self._require(actor, (Role.THERAPIST,), "暂停训练")
        case = self._cases.get(case_id)
        if case is None:
            raise NotFound("训练案例不存在")
        if case.status == "paused":
            raise Conflict("训练已处于暂停状态")
        self._commit(
            "TRAINING_PAUSED",
            "training_case",
            case_id,
            at,
            {"reason": reason, "paused_by": actor.actor_id},
        )

    def _on_training_paused(self, aggregate_id: str, payload: Mapping[str, Any], at: str) -> None:
        case = self._cases[aggregate_id]
        case.status = "paused"
        case.paused_by = str(payload["paused_by"])
        case.pause_reason = str(payload["reason"])

    def resume_training(self, actor: Actor, case_id: str, conclusion: str, at: str) -> None:
        """解除临床暂停：须由另一名有资质人员确认并记录复训结论。"""
        self._require(actor, _QUALIFIED_ROLES, "解除临床暂停")
        case = self._cases.get(case_id)
        if case is None:
            raise NotFound("训练案例不存在")
        if case.status != "paused":
            raise Conflict("训练未处于暂停状态")
        if actor.actor_id == case.paused_by:
            raise PermissionDenied("解除临床暂停须由另一名有资质人员确认")
        self._commit(
            "TRAINING_RESUMED",
            "training_case",
            case_id,
            at,
            {"confirmed_by": actor.actor_id, "conclusion": conclusion},
        )

    def _on_training_resumed(self, aggregate_id: str, payload: Mapping[str, Any], at: str) -> None:
        case = self._cases[aggregate_id]
        case.status = "active"
        case.resumed_by = str(payload["confirmed_by"])
        case.resume_conclusion = str(payload["conclusion"])

    # ------------------------------------------------------------------
    # 到期复核
    # ------------------------------------------------------------------

    def due_followups(self, now: str) -> list[_FollowUp]:
        """返回到期且未签署的复核，按到期时间排序。"""
        moment = _parse_moment(now)
        due = [
            followup
            for followup in self._followups.values()
            if followup.status == "pending" and _parse_moment(followup.due_at) <= moment
        ]
        return sorted(due, key=lambda followup: followup.due_at)

    def sign_review(self, actor: Actor, followup_id: str, decision: str, at: str) -> None:
        """有资质复核人员签署到期复核结论。"""
        self._require(actor, (Role.REVIEWER,), "签署复核")
        followup = self._followups.get(followup_id)
        if followup is None:
            raise NotFound("复核任务不存在")
        if followup.status == "signed":
            return
        self._commit(
            "REVIEW_SIGNED",
            "safety_review",
            followup_id,
            at,
            {"decision": decision, "reviewer_id": actor.actor_id},
        )

    def _on_review_signed(self, aggregate_id: str, payload: Mapping[str, Any], at: str) -> None:
        followup = self._followups[aggregate_id]
        followup.status = "signed"
        followup.signed_by = str(payload["reviewer_id"])
        followup.decision = str(payload["decision"])

    # ------------------------------------------------------------------
    # 查询：决策解释、患者视图、责任记录
    # ------------------------------------------------------------------

    def explain_session(self, session_id: str) -> str:
        """说明某次训练为何放行或暂停。"""
        decision = self._decisions.get(session_id)
        if decision is None:
            raise NotFound("该训练尚未评估")
        return decision.explain()

    def get_decision(self, session_id: str) -> Decision | None:
        return self._decisions.get(session_id)

    def patient_view(self, patient_id: str) -> dict[str, Any]:
        """患者可读的简明授权与训练状态。"""
        consent = self._consents.get(patient_id)
        if consent is None:
            return {
                "patient_id": patient_id,
                "consent_status": "unknown",
                "consent_label": "未记录授权",
                "training_status": "none",
                "training_label": "暂无训练安排",
                "message": "尚未记录授权，请联系康复科。",
            }
        if consent.status == "withdrawn":
            return {
                "patient_id": patient_id,
                "consent_status": "withdrawn",
                "consent_label": "授权已撤回",
                "training_status": "stopped",
                "training_label": "已停止新训练",
                "message": "授权已撤回：不再安排新训练，既有责任记录依法保留，详细记录仅法规审计可查。",
            }
        cases = [case for case in self._cases.values() if case.patient_id == patient_id]
        pending = sum(
            1
            for followup in self._followups.values()
            if followup.status == "pending" and any(c.case_id == followup.case_id for c in cases)
        )
        if any(case.status == "paused" for case in cases):
            status, label, message = "paused", "训练已暂停", "训练已由治疗师暂停，等待有资质人员确认复训。"
        elif cases:
            status, label, message = "active", "训练进行中", "授权有效，按预约参加训练。"
        else:
            status, label, message = "none", "暂无训练安排", "授权有效，尚未开立训练案例。"
        return {
            "patient_id": patient_id,
            "consent_status": "active",
            "consent_label": "授权有效",
            "training_status": status,
            "training_label": label,
            "pending_followups": pending,
            "message": message,
        }

    def case_history(self, actor: Actor, case_id: str) -> list[dict[str, Any]]:
        """训练案例的可追溯责任记录；患者撤回授权后仅法规审计可查。"""
        case = self._cases.get(case_id)
        if case is None:
            raise NotFound("训练案例不存在")
        consent = self._consents.get(case.patient_id)
        if consent is not None and consent.status == "withdrawn":
            self._require(actor, (Role.AUDITOR,), "查看已撤回授权患者的既往记录")
        else:
            self._staff_of(actor)
        session_ids = {s.session_id for s in self._sessions.values() if s.case_id == case_id}
        followup_ids = {f.followup_id for f in self._followups.values() if f.case_id == case_id}
        related = session_ids | followup_ids | {case_id}
        return [event for event in self._ledger.events() if event["aggregate_id"] in related]
