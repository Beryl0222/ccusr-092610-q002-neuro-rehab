"""端到端演示：一次完整的评估、暂停、解除与撤回流程。

运行：PYTHONPATH=src python3 -m neuro_rehab.demo
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .actors import Actor, Role
from .service import TrainingDecisionService

T = "2026-09-26T08:00:00+08:00"

OP = Actor("op-1", frozenset({Role.OPERATOR}))
TH1 = Actor("th-1", frozenset({Role.THERAPIST}))
TH2 = Actor("th-2", frozenset({Role.THERAPIST}))
REV = Actor("rev-1", frozenset({Role.REVIEWER}))
MT = Actor("mt-1", frozenset({Role.DEVICE_MAINTENANCE}))
PATIENT = Actor("patient-1", frozenset({Role.PATIENT}))


def show(title: str, payload) -> None:
    print(f"\n== {title} ==")
    if isinstance(payload, dict):
        for key, value in payload.items():
            print(f"  {key}: {value}")
    else:
        print(f"  {payload}")


def main() -> int:
    store = Path(tempfile.mkdtemp(prefix="neuro-rehab-")) / "events.jsonl"
    svc = TrainingDecisionService(store)
    print(f"事件账：{store}")

    svc.register_staff(OP, "op-1", ["operator"], T)
    svc.register_staff(OP, "th-1", ["therapist"], T)
    svc.register_staff(OP, "th-2", ["therapist"], T)
    svc.register_staff(OP, "rev-1", ["reviewer"], T)
    svc.register_staff(OP, "mt-1", ["device_maintenance"], T)
    svc.register_venue(OP, "venue-1", 1, T)
    print("已登记工作人员与场地")

    svc.record_consent(TH1, "patient-1", T)
    svc.approve_device_profile(REV, "dev-1", "revA", "dec-1.0", T)
    svc.record_baseline(TH1, "patient-1", "base-1", T)
    svc.place_booking(
        OP, "bk-1", "patient-1", "th-1", "venue-1", "dev-1",
        "2026-09-27T09:00:00+08:00", "2026-09-27T10:00:00+08:00", T,
    )
    svc.record_therapist_approval(TH1, "bk-1", T)
    print("已记录授权、设备版本、基线、预约与治疗师批准")

    result = svc.evaluate_booking(TH1, "bk-1", "2026-09-26T10:00:00+08:00")
    show("首次评估", result["explanation"]["summary"])

    svc.pause_training(TH1, "patient-1", "患者疲劳，信号质量下降", "2026-09-26T10:05:00+08:00")
    result = svc.evaluate_booking(TH1, "bk-1", "2026-09-26T10:06:00+08:00")
    explanation = svc.explain_decision(result["event_id"])
    show("暂停后评估", explanation["summary"])
    for gate in explanation["failed_gates"]:
        print(f"  - [{gate['code']}] {gate['message']}")

    svc.report_device_status(MT, "dev-1", "ok", "2026-09-26T10:10:00+08:00", note="例行检查正常")
    svc.confirm_resume(TH2, "patient-1", "2026-09-26T10:30:00+08:00")
    result = svc.evaluate_booking(TH1, "bk-1", "2026-09-26T10:31:00+08:00")
    show("另一名有资质人员确认解除后", result["explanation"]["summary"])

    svc.record_session_held(TH1, "bk-1", "常规训练", "2026-09-27T09:40:00+08:00", "2026-09-27T09:40:00+08:00")
    accepted = svc.ingest_summary(TH1, "bk-1", "v1", {"rms": 0.5, "artifacts": 3}, "2026-09-27T09:41:00+08:00")
    duplicate = svc.ingest_summary(TH1, "bk-1", "v1", {"rms": 0.5, "artifacts": 3}, "2026-09-27T09:42:00+08:00")
    conflict = svc.ingest_summary(TH1, "bk-1", "v1", {"rms": 0.9}, "2026-09-27T09:43:00+08:00")
    show("训练摘要", f"首次 {accepted['status']} → 重复 {duplicate['status']} → 同版本不同内容 {conflict['status']}")

    svc.withdraw_consent(PATIENT, "patient-1", "2026-09-27T11:00:00+08:00")
    show("患者撤回授权后的患者视图", svc.patient_view("patient-1", "2026-09-27T11:00:00+08:00"))

    restarted = TrainingDecisionService(store)
    print(f"\n服务重启后重放 {restarted.event_count} 条事件，到期复核补发：{restarted.recover('2026-09-27T12:00:00+08:00')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
