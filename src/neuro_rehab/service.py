"""训练决策服务。

职责边界：只管理评估与授权，不接入硬件，不替代医疗判断。

- 全部状态变化以契约事件入账（仅追加），服务重启后重放恢复；
- 治疗师可暂停训练，设备维护人员只能报告设备状态，
  解除临床暂停必须由另一名有资质人员确认；
- 设备或算法版本更新只影响新评估，不追改既往结论；
- 患者撤回授权后停止新训练并限制后续查看，既有责任记录依法保留；
- 重复上传同一训练摘要按指纹幂等，版本相同但内容变化的摘要隔离；
- 预约并发不得超过治疗师与场地容量；
- 每次评估都给出放行或暂停的结构化原因。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .actors import QUALIFIED_ROLES, Actor, Role, parse_roles
from .contracts import validate_event
from .decisions import (
    PAUSED,
    RELEASED,
    decision_fingerprint,
    explain_decision,
    fail,
    ok,
    summary_fingerprint,
)
from .errors import (
    CapacityExceeded,
    NotFound,
    PermissionDenied,
    ServiceError,
    StateError,
)
from .store import EventStore
from .views import filter_history, render_patient_view

DEVICE_STATUS_LABELS = {"ok": "正常", "maintenance": "维护中", "fault": "故障"}
REVIEW_DECISIONS = {"continue", "retrain", "suspend"}

_DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"
)


def _load_default_schema() -> dict[str, Any] | None:
    try:
        return json.loads(_DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8"))
    except OSError:
        return None


def _parse_ts(value: Any, field: str) -> datetime:
    """解析必须携带时区的 ISO 时间，非法输入抛出参数错误。"""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ServiceError(f"{field} 时间格式无效：{value!r}", code="invalid_timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ServiceError(f"{field} 必须携带时区", code="invalid_timestamp")
    return parsed


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


class TrainingDecisionService:
    """训练决策服务：评估、授权、暂停、复核与可追溯记录。"""

    def __init__(
        self,
        store_path: str | Path | None = None,
        *,
        schema: Mapping[str, Any] | None = None,
    ) -> None:
        self._schema = dict(schema) if schema is not None else _load_default_schema()
        validator: Callable[[Mapping[str, Any]], list] | None = None
        if self._schema is not None:
            validator = lambda event: validate_event(event, self._schema)  # noqa: E731
        self._store = EventStore(store_path, validator=validator)
        self._lock = threading.RLock()
        self._reset()
        for event in self._store.replay():
            self._apply(event)

    # ------------------------------------------------------------------
    # 投影
    # ------------------------------------------------------------------

    def _reset(self) -> None:
        self._seq = 0
        self._versions: dict[tuple[str, str], int] = {}
        self._staff: dict[str, dict[str, Any]] = {}
        self._venues: dict[str, dict[str, Any]] = {}
        self._consents: dict[str, dict[str, Any]] = {}
        self._profiles: dict[str, list[dict[str, Any]]] = {}
        self._device_status: dict[str, dict[str, Any]] = {}
        self._baselines: dict[str, dict[str, Any]] = {}
        self._bookings: dict[str, dict[str, Any]] = {}
        self._approvals: dict[str, dict[str, Any]] = {}
        self._decisions: dict[str, list[dict[str, Any]]] = {}
        self._pauses: dict[str, dict[str, Any]] = {}
        self._summaries: dict[tuple[str, str], dict[str, Any]] = {}
        self._quarantine: list[dict[str, Any]] = []
        self._reviews: dict[str, dict[str, Any]] = {}
        self._followup_emitted: set[str] = set()

    def _apply(self, event: Mapping[str, Any]) -> None:
        event_type = event["event_type"]
        payload = event["payload"]
        occurred_at = event["occurred_at"]
        try:
            self._seq = max(self._seq, int(str(event["event_id"]).rsplit("-", 1)[-1]))
        except ValueError:
            pass  # 外部入账的事件标识不参与序号推进
        key = (event["aggregate_type"], event["aggregate_id"])
        self._versions[key] = max(self._versions.get(key, 0), int(event["version"]))

        if event_type == "STAFF_REGISTERED":
            self._staff[payload["staff_id"]] = {
                "staff_id": payload["staff_id"],
                "roles": parse_roles(list(payload["roles"])),
                "max_concurrent": int(payload.get("max_concurrent", 1)),
                "name": payload.get("name"),
            }
        elif event_type == "VENUE_REGISTERED":
            self._venues[payload["venue_id"]] = {
                "venue_id": payload["venue_id"],
                "capacity": int(payload["capacity"]),
                "condition": "ok",
            }
        elif event_type == "VENUE_CONDITION_RECORDED":
            venue = self._venues.setdefault(
                payload["venue_id"],
                {"venue_id": payload["venue_id"], "capacity": 0, "condition": "ok"},
            )
            venue["condition"] = payload["condition"]
        elif event_type == "CONSENT_RECORDED":
            self._consents[payload["patient_id"]] = {
                "status": "active",
                "recorded_at": occurred_at,
                "scope": payload.get("scope"),
                "expires_at": payload.get("expires_at"),
            }
        elif event_type == "CONSENT_WITHDRAWN":
            consent = self._consents.setdefault(payload["patient_id"], {})
            consent.update({"status": "withdrawn", "withdrawn_at": occurred_at})
        elif event_type == "PROFILE_APPROVED":
            self._profiles.setdefault(payload["device_id"], []).append(
                {
                    "device_revision": payload["device_revision"],
                    "decoder_version": payload["decoder_version"],
                    "reviewer_id": payload["reviewer_id"],
                    "approved_at": occurred_at,
                }
            )
        elif event_type == "DEVICE_STATUS_REPORTED":
            self._device_status[payload["device_id"]] = {
                "status": payload["status"],
                "reporter_id": payload["reporter_id"],
                "note": payload.get("note"),
                "reported_at": occurred_at,
            }
        elif event_type == "BASELINE_RECORDED":
            self._baselines[payload["patient_id"]] = {
                "baseline_version": payload["baseline_version"],
                "recorded_at": occurred_at,
            }
        elif event_type == "BOOKING_PLACED":
            self._bookings[payload["booking_id"]] = {
                "booking_id": payload["booking_id"],
                "patient_id": payload["patient_id"],
                "therapist_id": payload["therapist_id"],
                "venue_id": payload["venue_id"],
                "device_id": payload["device_id"],
                "start": payload["start"],
                "end": payload["end"],
                "status": "active",
                "held": False,
            }
        elif event_type == "THERAPIST_APPROVAL_RECORDED":
            self._approvals[payload["booking_id"]] = {
                "therapist_id": payload["therapist_id"],
                "at": occurred_at,
                "event_id": event["event_id"],
            }
        elif event_type == "SESSION_DECIDED":
            self._decisions.setdefault(payload["booking_id"], []).append(
                {
                    "event_id": event["event_id"],
                    "occurred_at": occurred_at,
                    "payload": dict(payload),
                }
            )
        elif event_type == "SESSION_HELD":
            booking = self._bookings.get(payload["booking_id"])
            if booking is not None:
                booking["held"] = True
        elif event_type == "PAUSE_RECORDED":
            self._pauses[payload["pause_id"]] = {
                "pause_id": payload["pause_id"],
                "patient_id": payload["patient_id"],
                "paused_by": payload["paused_by"],
                "reason": payload["reason"],
                "paused_at": occurred_at,
                "status": "open",
            }
        elif event_type == "RESUME_CONFIRMED":
            pause = self._pauses.get(payload["pause_id"])
            if pause is not None:
                pause.update(
                    {
                        "status": "resumed",
                        "confirmed_by": payload["confirmed_by"],
                        "confirmed_at": occurred_at,
                    }
                )
        elif event_type == "SUMMARY_INGESTED":
            self._summaries[(payload["session_id"], payload["summary_version"])] = {
                "status": "accepted",
                "fingerprint": payload["fingerprint"],
                "patient_id": payload.get("patient_id"),
                "event_id": event["event_id"],
            }
        elif event_type == "SUMMARY_QUARANTINED":
            self._quarantine.append(
                {
                    "session_id": payload["session_id"],
                    "summary_version": payload["summary_version"],
                    "fingerprint": payload["fingerprint"],
                    "conflict_with": payload["conflict_with"],
                    "patient_id": payload.get("patient_id"),
                    "event_id": event["event_id"],
                    "occurred_at": occurred_at,
                }
            )
        elif event_type == "REVIEW_SCHEDULED":
            self._reviews[payload["review_id"]] = {
                "review_id": payload["review_id"],
                "patient_id": payload["patient_id"],
                "due_at": payload["due_at"],
                "status": "pending",
            }
        elif event_type == "FOLLOWUP_DUE":
            self._followup_emitted.add(payload["review_id"])
        elif event_type == "REVIEW_SIGNED":
            review = self._reviews.get(payload["review_id"])
            if review is not None:
                review.update(
                    {
                        "status": "signed",
                        "decision": payload["decision"],
                        "reviewer_id": payload["reviewer_id"],
                        "signed_at": occurred_at,
                    }
                )

    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            key = (aggregate_type, aggregate_id)
            event = {
                "event_id": f"evt-{self._seq:08d}",
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "occurred_at": occurred_at,
                "version": self._versions.get(key, 0) + 1,
                "payload": dict(payload),
            }
            record = self._store.append(event)
            self._apply(record)
            return record

    # ------------------------------------------------------------------
    # 权限
    # ------------------------------------------------------------------

    def _authorize(self, actor: Actor | None, allowed: Iterable[Role], action: str) -> None:
        if actor is None:
            raise PermissionDenied(f"{action}：缺少操作者")
        registered = self._staff.get(actor.actor_id)
        if registered is not None:
            roles = registered["roles"]
        elif Role.PATIENT in actor.roles:
            roles = actor.roles  # 患者本人（如撤回授权）
        elif not self._staff and Role.OPERATOR in actor.roles:
            roles = actor.roles  # 首次登记前的引导操作员
        else:
            raise PermissionDenied(f"{action}：操作者未登记")
        if not (set(allowed) & roles):
            raise PermissionDenied(
                f"{action}：当前角色无权执行该操作",
                details={"required": sorted(role.value for role in allowed)},
            )

    # ------------------------------------------------------------------
    # 登记
    # ------------------------------------------------------------------

    def register_staff(
        self,
        actor: Actor | None,
        staff_id: str,
        roles: Iterable[str],
        occurred_at: str,
        *,
        max_concurrent: int = 1,
        name: str | None = None,
    ) -> dict[str, Any]:
        """登记工作人员及角色；首次登记（引导）不要求操作员已存在。"""
        if self._staff:
            self._authorize(actor, {Role.OPERATOR}, "登记工作人员")
        elif actor is None or Role.OPERATOR not in actor.roles:
            raise PermissionDenied("登记工作人员：首次登记须由操作员发起")
        try:
            parsed = parse_roles(list(roles))
        except ValueError as exc:
            raise ServiceError(f"登记工作人员：未知角色（{exc}）", code="unknown_role")
        if not parsed:
            raise ServiceError("登记工作人员：至少需要一个角色", code="unknown_role")
        if max_concurrent < 1:
            raise ServiceError("登记工作人员：并发容量必须为正整数", code="invalid_capacity")
        return self._emit(
            "STAFF_REGISTERED",
            "staff_profile",
            staff_id,
            occurred_at,
            {
                "staff_id": staff_id,
                "roles": sorted(role.value for role in parsed),
                "registered_by": actor.actor_id,
                "max_concurrent": max_concurrent,
                "name": name,
            },
        )

    def register_venue(
        self,
        actor: Actor | None,
        venue_id: str,
        capacity: int,
        occurred_at: str,
    ) -> dict[str, Any]:
        self._authorize(actor, {Role.OPERATOR}, "登记场地")
        if capacity < 1:
            raise ServiceError("登记场地：容量必须为正整数", code="invalid_capacity")
        return self._emit(
            "VENUE_REGISTERED",
            "venue_profile",
            venue_id,
            occurred_at,
            {"venue_id": venue_id, "capacity": capacity},
        )

    def record_venue_condition(
        self,
        actor: Actor | None,
        venue_id: str,
        condition: str,
        occurred_at: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        self._authorize(actor, {Role.OPERATOR}, "登记场地状态")
        if venue_id not in self._venues:
            raise NotFound(f"场地未登记：{venue_id}")
        if not condition or not condition.strip():
            raise ServiceError("场地状态不能为空", code="invalid_condition")
        return self._emit(
            "VENUE_CONDITION_RECORDED",
            "venue_profile",
            venue_id,
            occurred_at,
            {
                "venue_id": venue_id,
                "condition": condition,
                "recorded_by": actor.actor_id,
                "note": note,
            },
        )

    # ------------------------------------------------------------------
    # 授权与设备
    # ------------------------------------------------------------------

    def record_consent(
        self,
        actor: Actor | None,
        patient_id: str,
        occurred_at: str,
        *,
        scope: str = "training",
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        self._authorize(actor, {Role.THERAPIST, Role.OPERATOR}, "记录患者授权")
        if expires_at is not None:
            _parse_ts(expires_at, "授权到期时间")
        return self._emit(
            "CONSENT_RECORDED",
            "patient_consent",
            patient_id,
            occurred_at,
            {
                "patient_id": patient_id,
                "recorded_by": actor.actor_id,
                "scope": scope,
                "expires_at": expires_at,
            },
        )

    def withdraw_consent(
        self,
        actor: Actor | None,
        patient_id: str,
        occurred_at: str,
    ) -> dict[str, Any]:
        """撤回授权：停止新训练并限制后续查看，既有责任记录保留。"""
        if actor is None:
            raise PermissionDenied("撤回授权：缺少操作者")
        is_self = Role.PATIENT in actor.roles and actor.actor_id == patient_id
        if not is_self:
            self._authorize(actor, {Role.OPERATOR}, "撤回授权")
        consent = self._consents.get(patient_id)
        if not consent or consent.get("status") != "active":
            raise StateError("撤回授权：当前没有处于有效状态的授权")
        return self._emit(
            "CONSENT_WITHDRAWN",
            "patient_consent",
            patient_id,
            occurred_at,
            {"patient_id": patient_id, "withdrawn_by": actor.actor_id},
        )

    def approve_device_profile(
        self,
        actor: Actor | None,
        device_id: str,
        device_revision: str,
        decoder_version: str,
        occurred_at: str,
    ) -> dict[str, Any]:
        """批准设备与解码器版本；新版本只影响后续评估。"""
        self._authorize(actor, {Role.REVIEWER}, "批准设备版本")
        return self._emit(
            "PROFILE_APPROVED",
            "device_profile",
            device_id,
            occurred_at,
            {
                "device_id": device_id,
                "device_revision": device_revision,
                "decoder_version": decoder_version,
                "reviewer_id": actor.actor_id,
            },
        )

    def report_device_status(
        self,
        actor: Actor | None,
        device_id: str,
        status: str,
        occurred_at: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """设备维护人员只能报告设备状态，不参与临床决定。"""
        self._authorize(actor, {Role.DEVICE_MAINTENANCE}, "报告设备状态")
        if status not in DEVICE_STATUS_LABELS:
            raise ServiceError(
                f"报告设备状态：未知状态 {status!r}",
                code="invalid_status",
                details={"allowed": sorted(DEVICE_STATUS_LABELS)},
            )
        return self._emit(
            "DEVICE_STATUS_REPORTED",
            "device_profile",
            device_id,
            occurred_at,
            {
                "device_id": device_id,
                "reporter_id": actor.actor_id,
                "status": status,
                "note": note,
            },
        )

    def record_baseline(
        self,
        actor: Actor | None,
        patient_id: str,
        baseline_version: str,
        occurred_at: str,
        *,
        notes: str | None = None,
    ) -> dict[str, Any]:
        self._authorize(actor, {Role.THERAPIST}, "记录训练基线")
        return self._emit(
            "BASELINE_RECORDED",
            "training_case",
            patient_id,
            occurred_at,
            {
                "patient_id": patient_id,
                "baseline_version": baseline_version,
                "recorded_by": actor.actor_id,
                "notes": notes,
            },
        )

    # ------------------------------------------------------------------
    # 预约与评估
    # ------------------------------------------------------------------

    def place_booking(
        self,
        actor: Actor | None,
        booking_id: str,
        patient_id: str,
        therapist_id: str,
        venue_id: str,
        device_id: str,
        start: str,
        end: str,
        occurred_at: str,
    ) -> dict[str, Any]:
        """预约训练：并发不得超过治疗师与场地容量。"""
        self._authorize(actor, {Role.OPERATOR, Role.THERAPIST}, "预约训练")
        start_at = _parse_ts(start, "开始时间")
        end_at = _parse_ts(end, "结束时间")
        if not start_at < end_at:
            raise ServiceError("预约训练：开始时间必须早于结束时间", code="invalid_window")
        with self._lock:
            if booking_id in self._bookings:
                raise StateError(f"预约训练：预约标识已存在 {booking_id}")
            consent = self._consents.get(patient_id)
            if not consent or consent.get("status") != "active":
                raise StateError("预约训练：患者授权未处于有效状态，不能预约新训练")
            therapist = self._staff.get(therapist_id)
            if therapist is None or Role.THERAPIST not in therapist["roles"]:
                raise NotFound(f"预约训练：治疗师未登记 {therapist_id}")
            if venue_id not in self._venues:
                raise NotFound(f"预约训练：场地未登记 {venue_id}")
            self._assert_capacity(therapist_id, venue_id, start_at, end_at)
            return self._emit(
                "BOOKING_PLACED",
                "training_case",
                booking_id,
                occurred_at,
                {
                    "booking_id": booking_id,
                    "patient_id": patient_id,
                    "therapist_id": therapist_id,
                    "venue_id": venue_id,
                    "device_id": device_id,
                    "start": start,
                    "end": end,
                },
            )

    def _assert_capacity(
        self,
        therapist_id: str,
        venue_id: str,
        start_at: datetime,
        end_at: datetime,
    ) -> None:
        therapist = self._staff[therapist_id]
        overlapping_staff = [
            b
            for b in self._bookings.values()
            if b["status"] == "active"
            and b["therapist_id"] == therapist_id
            and _overlaps(start_at, end_at, _parse_ts(b["start"], "start"), _parse_ts(b["end"], "end"))
        ]
        if len(overlapping_staff) >= therapist["max_concurrent"]:
            raise CapacityExceeded(
                "预约训练：治疗师并发容量不足",
                details={
                    "therapist_id": therapist_id,
                    "max_concurrent": therapist["max_concurrent"],
                    "conflicts": [b["booking_id"] for b in overlapping_staff],
                },
            )
        venue = self._venues[venue_id]
        overlapping_venue = [
            b
            for b in self._bookings.values()
            if b["status"] == "active"
            and b["venue_id"] == venue_id
            and _overlaps(start_at, end_at, _parse_ts(b["start"], "start"), _parse_ts(b["end"], "end"))
        ]
        if len(overlapping_venue) >= venue["capacity"]:
            raise CapacityExceeded(
                "预约训练：场地并发容量不足",
                details={
                    "venue_id": venue_id,
                    "capacity": venue["capacity"],
                    "conflicts": [b["booking_id"] for b in overlapping_venue],
                },
            )

    def record_therapist_approval(
        self,
        actor: Actor | None,
        booking_id: str,
        occurred_at: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        self._authorize(actor, {Role.THERAPIST}, "记录治疗师批准")
        if booking_id not in self._bookings:
            raise NotFound(f"记录治疗师批准：预约不存在 {booking_id}")
        return self._emit(
            "THERAPIST_APPROVAL_RECORDED",
            "training_case",
            booking_id,
            occurred_at,
            {"booking_id": booking_id, "therapist_id": actor.actor_id, "note": note},
        )

    def evaluate_booking(
        self,
        actor: Actor | None,
        booking_id: str,
        occurred_at: str,
    ) -> dict[str, Any]:
        """评估预约并给出放行或暂停的结构化原因；相同结论不重复入账。"""
        self._authorize(actor, {Role.THERAPIST, Role.OPERATOR, Role.REVIEWER}, "评估训练")
        now = _parse_ts(occurred_at, "评估时间")
        with self._lock:
            booking = self._bookings.get(booking_id)
            if booking is None:
                raise NotFound(f"评估训练：预约不存在 {booking_id}")
            gates, snapshot = self._evaluate_gates(booking, now)
            decision = RELEASED if all(gate.passed for gate in gates) else PAUSED
            payload: dict[str, Any] = {
                "booking_id": booking_id,
                "patient_id": booking["patient_id"],
                "decision": decision,
                "gates": [gate.to_dict() for gate in gates],
                "therapist_id": booking["therapist_id"],
                "approval_event_id": (
                    self._approvals.get(booking_id, {}).get("event_id")
                ),
                **snapshot,
            }
            payload["fingerprint"] = decision_fingerprint(payload)
            history = self._decisions.get(booking_id, [])
            if history and history[-1]["payload"].get("fingerprint") == payload["fingerprint"]:
                last = history[-1]
                return {
                    "event_id": last["event_id"],
                    "decision": decision,
                    "deduplicated": True,
                    "explanation": explain_decision(last["payload"]),
                }
            record = self._emit(
                "SESSION_DECIDED", "training_case", booking_id, occurred_at, payload
            )
            return {
                "event_id": record["event_id"],
                "decision": decision,
                "deduplicated": False,
                "explanation": explain_decision(record["payload"]),
            }

    def _evaluate_gates(self, booking: Mapping[str, Any], now: datetime):
        gates = []
        patient_id = booking["patient_id"]

        consent = self._consents.get(patient_id)
        if not consent or "status" not in consent:
            consent_status = "missing"
            gates.append(fail("consent", "consent_missing", "未记录患者授权"))
        elif consent["status"] == "withdrawn":
            consent_status = "withdrawn"
            gates.append(fail("consent", "consent_withdrawn", "患者已撤回授权，停止新训练"))
        elif consent.get("expires_at") and _parse_ts(consent["expires_at"], "授权到期时间") <= now:
            consent_status = "expired"
            gates.append(fail("consent", "consent_expired", "患者授权已过期"))
        else:
            consent_status = "active"
            gates.append(ok("consent", "患者授权有效"))

        profile = self._latest_profile(booking["device_id"])
        device_revision = decoder_version = None
        if profile is None:
            gates.append(fail("device", "device_profile_missing", "设备未登记已批准的版本档案"))
        else:
            device_revision = profile["device_revision"]
            decoder_version = profile["decoder_version"]
            status = self._device_status.get(booking["device_id"])
            if status is not None and status["status"] != "ok":
                label = DEVICE_STATUS_LABELS.get(status["status"], status["status"])
                gates.append(
                    fail("device", "device_unavailable", f"设备当前不可用：{label}（维护人员报告）")
                )
            else:
                gates.append(
                    ok("device", f"设备版本 {device_revision} / 解码器 {decoder_version} 已批准且可用")
                )

        baseline = self._baselines.get(patient_id)
        baseline_version = None
        if baseline is None:
            gates.append(fail("baseline", "baseline_missing", "未记录个体训练基线"))
        else:
            baseline_version = baseline["baseline_version"]
            gates.append(ok("baseline", f"个体训练基线 {baseline_version} 已记录"))

        venue = self._venues.get(booking["venue_id"])
        venue_condition = venue["condition"] if venue else None
        if venue is None:
            gates.append(fail("venue", "venue_missing", "场地未登记"))
        elif venue["condition"] != "ok":
            gates.append(
                fail("venue", "venue_condition_not_ok", f"场地当前状态不可用：{venue['condition']}")
            )
        else:
            gates.append(ok("venue", "场地条件正常"))

        approval = self._approvals.get(booking["booking_id"])
        if approval is None:
            gates.append(
                fail("therapist_approval", "therapist_approval_missing", "缺少治疗师对本节训练的批准")
            )
        else:
            gates.append(ok("therapist_approval", f"治疗师 {approval['therapist_id']} 已批准"))

        pause = self._open_pause(patient_id)
        if pause is not None:
            gates.append(
                fail("clinical_pause", "clinical_pause_active", f"存在未解除的临床暂停：{pause['reason']}")
            )
        else:
            gates.append(ok("clinical_pause", "无未解除的临床暂停"))

        overdue = self._overdue_review(patient_id, now)
        if overdue is not None:
            gates.append(
                fail("review", "review_overdue", f"复核已到期（{overdue['due_at']}），完成前暂停放行")
            )
        else:
            gates.append(ok("review", "无到期未完成的复核"))

        snapshot = {
            "device_revision": device_revision,
            "decoder_version": decoder_version,
            "baseline_version": baseline_version,
            "venue_condition": venue_condition,
            "consent_status": consent_status,
        }
        return gates, snapshot

    def record_session_held(
        self,
        actor: Actor | None,
        booking_id: str,
        reason: str,
        observed_at: str,
        occurred_at: str,
    ) -> dict[str, Any]:
        """记录已完成的训练；仅最近一次评估放行且无未解除暂停时可入账。"""
        self._authorize(actor, {Role.THERAPIST}, "记录训练")
        _parse_ts(observed_at, "观察时间")
        with self._lock:
            booking = self._bookings.get(booking_id)
            if booking is None:
                raise NotFound(f"记录训练：预约不存在 {booking_id}")
            history = self._decisions.get(booking_id, [])
            if not history or history[-1]["payload"]["decision"] != RELEASED:
                raise StateError("记录训练：最近一次评估未放行，不能记录训练")
            if self._open_pause(booking["patient_id"]) is not None:
                raise StateError("记录训练：存在未解除的临床暂停")
            consent = self._consents.get(booking["patient_id"], {})
            if consent.get("status") != "active":
                raise StateError("记录训练：患者授权未处于有效状态")
            return self._emit(
                "SESSION_HELD",
                "training_case",
                booking_id,
                occurred_at,
                {"booking_id": booking_id, "reason": reason, "observed_at": observed_at},
            )

    # ------------------------------------------------------------------
    # 临床暂停
    # ------------------------------------------------------------------

    def pause_training(
        self,
        actor: Actor | None,
        patient_id: str,
        reason: str,
        occurred_at: str,
    ) -> dict[str, Any]:
        """治疗师暂停训练。"""
        self._authorize(actor, {Role.THERAPIST}, "暂停训练")
        if not reason or not reason.strip():
            raise ServiceError("暂停训练：必须说明暂停原因", code="reason_required")
        with self._lock:
            if self._open_pause(patient_id) is not None:
                raise StateError("暂停训练：已存在未解除的临床暂停")
            seq = sum(1 for p in self._pauses.values() if p["patient_id"] == patient_id) + 1
            pause_id = f"pause-{patient_id}-{seq}"
            return self._emit(
                "PAUSE_RECORDED",
                "training_case",
                patient_id,
                occurred_at,
                {
                    "patient_id": patient_id,
                    "pause_id": pause_id,
                    "paused_by": actor.actor_id,
                    "reason": reason,
                },
            )

    def confirm_resume(
        self,
        actor: Actor | None,
        patient_id: str,
        occurred_at: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """解除临床暂停：必须由另一名有资质人员确认。"""
        self._authorize(actor, QUALIFIED_ROLES, "解除临床暂停")
        with self._lock:
            pause = self._open_pause(patient_id)
            if pause is None:
                raise StateError("解除临床暂停：无待解除的临床暂停")
            if actor.actor_id == pause["paused_by"]:
                raise PermissionDenied("解除临床暂停：须由另一名有资质人员确认")
            return self._emit(
                "RESUME_CONFIRMED",
                "training_case",
                patient_id,
                occurred_at,
                {
                    "patient_id": patient_id,
                    "pause_id": pause["pause_id"],
                    "confirmed_by": actor.actor_id,
                    "note": note,
                },
            )

    # ------------------------------------------------------------------
    # 训练摘要（幂等与隔离）
    # ------------------------------------------------------------------

    def ingest_summary(
        self,
        actor: Actor | None,
        session_id: str,
        summary_version: str,
        content: Mapping[str, Any],
        occurred_at: str,
    ) -> dict[str, Any]:
        """接收训练摘要：同指纹幂等，同版本不同内容隔离。"""
        self._authorize(actor, {Role.THERAPIST, Role.OPERATOR}, "接收训练摘要")
        if not isinstance(content, Mapping) or not content:
            raise ServiceError("接收训练摘要：摘要内容不能为空", code="invalid_summary")
        with self._lock:
            booking = self._bookings.get(session_id)
            if booking is None:
                raise NotFound(f"接收训练摘要：训练会话不存在 {session_id}")
            fingerprint = summary_fingerprint(content)
            key = (session_id, summary_version)
            existing = self._summaries.get(key)
            if existing is not None:
                if existing["fingerprint"] == fingerprint:
                    return {
                        "status": "duplicate",
                        "fingerprint": fingerprint,
                        "event_id": existing["event_id"],
                    }
                record = self._emit(
                    "SUMMARY_QUARANTINED",
                    "training_case",
                    session_id,
                    occurred_at,
                    {
                        "session_id": session_id,
                        "summary_version": summary_version,
                        "fingerprint": fingerprint,
                        "conflict_with": existing["fingerprint"],
                        "patient_id": booking["patient_id"],
                        "content": dict(content),
                    },
                )
                return {
                    "status": "quarantined",
                    "fingerprint": fingerprint,
                    "conflict_with": existing["fingerprint"],
                    "event_id": record["event_id"],
                }
            record = self._emit(
                "SUMMARY_INGESTED",
                "training_case",
                session_id,
                occurred_at,
                {
                    "session_id": session_id,
                    "summary_version": summary_version,
                    "fingerprint": fingerprint,
                    "patient_id": booking["patient_id"],
                    "content": dict(content),
                },
            )
            return {"status": "accepted", "fingerprint": fingerprint, "event_id": record["event_id"]}

    # ------------------------------------------------------------------
    # 复核
    # ------------------------------------------------------------------

    def schedule_review(
        self,
        actor: Actor | None,
        patient_id: str,
        due_at: str,
        occurred_at: str,
    ) -> dict[str, Any]:
        self._authorize(actor, {Role.THERAPIST, Role.REVIEWER, Role.OPERATOR}, "安排复核")
        _parse_ts(due_at, "复核到期时间")
        with self._lock:
            seq = sum(1 for r in self._reviews.values() if r["patient_id"] == patient_id) + 1
            review_id = f"review-{patient_id}-{seq}"
            record = self._emit(
                "REVIEW_SCHEDULED",
                "safety_review",
                review_id,
                occurred_at,
                {"review_id": review_id, "patient_id": patient_id, "due_at": due_at},
            )
            return {"review_id": review_id, "event_id": record["event_id"]}

    def sign_review(
        self,
        actor: Actor | None,
        review_id: str,
        decision: str,
        occurred_at: str,
        *,
        next_due_at: str | None = None,
    ) -> dict[str, Any]:
        """签署复核结论；结论为暂停时自动形成临床暂停。"""
        self._authorize(actor, {Role.REVIEWER}, "签署复核结论")
        if decision not in REVIEW_DECISIONS:
            raise ServiceError(
                f"签署复核结论：未知结论 {decision!r}",
                code="invalid_decision",
                details={"allowed": sorted(REVIEW_DECISIONS)},
            )
        if next_due_at is not None:
            _parse_ts(next_due_at, "下次复核到期时间")
        with self._lock:
            review = self._reviews.get(review_id)
            if review is None:
                raise NotFound(f"签署复核结论：复核不存在 {review_id}")
            if review["status"] != "pending":
                raise StateError("签署复核结论：复核已签署")
            record = self._emit(
                "REVIEW_SIGNED",
                "safety_review",
                review_id,
                occurred_at,
                {"review_id": review_id, "decision": decision, "reviewer_id": actor.actor_id},
            )
            if decision == "suspend":
                patient_id = review["patient_id"]
                if self._open_pause(patient_id) is None:
                    seq = sum(1 for p in self._pauses.values() if p["patient_id"] == patient_id) + 1
                    self._emit(
                        "PAUSE_RECORDED",
                        "training_case",
                        patient_id,
                        occurred_at,
                        {
                            "patient_id": patient_id,
                            "pause_id": f"pause-{patient_id}-{seq}",
                            "paused_by": actor.actor_id,
                            "reason": f"复核结论：暂停（{review_id}）",
                        },
                    )
            if next_due_at is not None:
                self.schedule_review(actor, review["patient_id"], next_due_at, occurred_at)
            return {"review_id": review_id, "event_id": record["event_id"]}

    def recover(self, occurred_at: str, actor: Actor | None = None) -> list[str]:
        """服务恢复后继续到期复核：为每个到期未复核记录补发到期事件（幂等）。"""
        if actor is not None:
            self._authorize(actor, {Role.OPERATOR}, "恢复到期复核")
        now = _parse_ts(occurred_at, "恢复时间")
        emitted: list[str] = []
        with self._lock:
            for review in sorted(self._reviews.values(), key=lambda r: r["due_at"]):
                if (
                    review["status"] == "pending"
                    and _parse_ts(review["due_at"], "复核到期时间") <= now
                    and review["review_id"] not in self._followup_emitted
                ):
                    self._emit(
                        "FOLLOWUP_DUE",
                        "safety_review",
                        review["review_id"],
                        occurred_at,
                        {
                            "review_id": review["review_id"],
                            "patient_id": review["patient_id"],
                            "due_at": review["due_at"],
                        },
                    )
                    emitted.append(review["review_id"])
        return emitted

    # ------------------------------------------------------------------
    # 查询与解释
    # ------------------------------------------------------------------

    def explain_decision(self, ref: str) -> dict[str, Any]:
        """按决定事件标识或预约标识解释某次训练为何放行或暂停。"""
        with self._lock:
            payload = None
            event_id = None
            occurred_at = None
            for history in self._decisions.values():
                for entry in history:
                    if entry["event_id"] == ref:
                        payload, event_id, occurred_at = (
                            entry["payload"],
                            entry["event_id"],
                            entry["occurred_at"],
                        )
            if payload is None:
                history = self._decisions.get(ref)
                if history:
                    entry = history[-1]
                    payload, event_id, occurred_at = (
                        entry["payload"],
                        entry["event_id"],
                        entry["occurred_at"],
                    )
            if payload is None:
                raise NotFound(f"解释评估决定：未找到决定 {ref}")
            explanation = explain_decision(payload)
            explanation["event_id"] = event_id
            explanation["occurred_at"] = occurred_at
            return explanation

    def decision_audit(self, decision_event_id: str) -> dict[str, Any]:
        """对比决定时与当前的版本快照；既往结论不因版本更新而改变。"""
        explanation = self.explain_decision(decision_event_id)
        booking = self._bookings.get(explanation["booking_id"])
        profile = self._latest_profile(booking["device_id"]) if booking else None
        baseline = self._baselines.get(explanation["patient_id"])
        return {
            "decision_event_id": explanation["event_id"],
            "decision": explanation["decision"],
            "decided_at": explanation["occurred_at"],
            "versions_at_decision": explanation["versions"],
            "versions_now": {
                "device_revision": profile["device_revision"] if profile else None,
                "decoder_version": profile["decoder_version"] if profile else None,
                "baseline_version": baseline["baseline_version"] if baseline else None,
            },
            "conclusion_changed": False,
            "note": "设备或算法版本更新不追改既往训练结论；如需按新版本评估请重新发起评估",
        }

    def patient_view(self, patient_id: str, at: str) -> dict[str, Any]:
        """患者简明视图：授权与训练状态。"""
        now = _parse_ts(at, "查询时间")
        with self._lock:
            consent = self._consents.get(patient_id, {})
            pending = [
                r for r in self._reviews.values() if r["patient_id"] == patient_id and r["status"] == "pending"
            ]
            next_review_at = min((r["due_at"] for r in pending), default=None)
            last_decision = None
            for booking_id, history in self._decisions.items():
                booking = self._bookings.get(booking_id)
                if booking and booking["patient_id"] == patient_id and history:
                    entry = history[-1]
                    if last_decision is None or entry["occurred_at"] > last_decision["occurred_at"]:
                        last_decision = entry
            facts = {
                "patient_id": patient_id,
                "consent_status": consent.get("status", "missing"),
                "active_pause": self._open_pause(patient_id),
                "overdue_review": self._overdue_review(patient_id, now),
                "next_review_at": next_review_at,
                "last_decision": (
                    {**last_decision["payload"], "occurred_at": last_decision["occurred_at"]}
                    if last_decision
                    else None
                ),
            }
            return render_patient_view(facts)

    def case_history(self, patient_id: str, *, purpose: str = "care") -> list[dict[str, Any]]:
        """患者相关历史；撤回授权后限制临床细节查看，责任记录保留。"""
        if purpose not in {"care", "legal"}:
            raise ServiceError("查看历史：目的必须是 care 或 legal", code="invalid_purpose")
        with self._lock:
            consent = self._consents.get(patient_id, {})
            withdrawn = consent.get("status") == "withdrawn"
            entries = []
            for event in self._store.replay():
                if self._event_patient_id(event) != patient_id:
                    continue
                entries.append(
                    {
                        "event_id": event["event_id"],
                        "event_type": event["event_type"],
                        "occurred_at": event["occurred_at"],
                        "summary": self._history_summary(event),
                    }
                )
            return filter_history(entries, consent_withdrawn=withdrawn, purpose=purpose)

    def list_quarantined(self) -> list[dict[str, Any]]:
        """被隔离的冲突摘要，供运营核对处理。"""
        with self._lock:
            return [dict(item) for item in self._quarantine]

    def get_booking(self, booking_id: str) -> dict[str, Any]:
        with self._lock:
            booking = self._bookings.get(booking_id)
            if booking is None:
                raise NotFound(f"预约不存在：{booking_id}")
            result = dict(booking)
            history = self._decisions.get(booking_id, [])
            result["last_decision"] = (
                history[-1]["payload"]["decision"] if history else None
            )
            return result

    @property
    def event_count(self) -> int:
        """已入账事件总数，用于幂等断言与运营监控。"""
        return len(self._store)

    # ------------------------------------------------------------------
    # 内部查询
    # ------------------------------------------------------------------

    def _latest_profile(self, device_id: str) -> dict[str, Any] | None:
        profiles = self._profiles.get(device_id)
        return profiles[-1] if profiles else None

    def _open_pause(self, patient_id: str) -> dict[str, Any] | None:
        for pause in self._pauses.values():
            if pause["patient_id"] == patient_id and pause["status"] == "open":
                return pause
        return None

    def _overdue_review(self, patient_id: str, now: datetime) -> dict[str, Any] | None:
        overdue = [
            review
            for review in self._reviews.values()
            if review["patient_id"] == patient_id
            and review["status"] == "pending"
            and _parse_ts(review["due_at"], "复核到期时间") <= now
        ]
        if not overdue:
            return None
        return min(overdue, key=lambda r: r["due_at"])

    def _event_patient_id(self, event: Mapping[str, Any]) -> str | None:
        payload = event["payload"]
        if "patient_id" in payload:
            return payload["patient_id"]
        booking_id = payload.get("booking_id")
        if booking_id and booking_id in self._bookings:
            return self._bookings[booking_id]["patient_id"]
        return None

    @staticmethod
    def _history_summary(event: Mapping[str, Any]) -> str:
        payload = event["payload"]
        event_type = event["event_type"]
        if event_type == "SESSION_DECIDED":
            failed = [g for g in payload.get("gates", []) if not g.get("passed")]
            if payload.get("decision") == RELEASED:
                return "评估放行：全部检查通过"
            return f"评估暂停：{len(failed)} 项检查未通过"
        if event_type == "PAUSE_RECORDED":
            return f"临床暂停：{payload.get('reason')}"
        if event_type == "RESUME_CONFIRMED":
            return f"解除暂停（确认人 {payload.get('confirmed_by')}）"
        if event_type == "REVIEW_SIGNED":
            return f"复核结论：{payload.get('decision')}"
        if event_type == "SUMMARY_QUARANTINED":
            return "摘要与已接收版本冲突，已隔离"
        if event_type == "CONSENT_WITHDRAWN":
            return "患者撤回授权"
        return ""
