"""面向患者的简明视图与撤回后的查看限制。

患者撤回授权后：停止新训练、限制后续查看，但依法需要的既有责任
记录（决定、暂停、复核、同意轨迹）仍然保留且可查。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

#: 事件类型 → 记录类别。accountability 为依法保留的责任记录；
#: clinical_detail 为撤回后限制查看的临床细节。
EVENT_CATEGORIES = {
    "CONSENT_RECORDED": "accountability",
    "CONSENT_WITHDRAWN": "accountability",
    "SESSION_DECIDED": "accountability",
    "SESSION_HELD": "accountability",
    "PAUSE_RECORDED": "accountability",
    "RESUME_CONFIRMED": "accountability",
    "REVIEW_SCHEDULED": "accountability",
    "FOLLOWUP_DUE": "accountability",
    "REVIEW_SIGNED": "accountability",
    "BASELINE_RECORDED": "clinical_detail",
    "SUMMARY_INGESTED": "clinical_detail",
    "SUMMARY_QUARANTINED": "clinical_detail",
}

EVENT_LABELS = {
    "CONSENT_RECORDED": "记录授权",
    "CONSENT_WITHDRAWN": "撤回授权",
    "SESSION_DECIDED": "训练评估",
    "SESSION_HELD": "完成训练",
    "PAUSE_RECORDED": "临床暂停",
    "RESUME_CONFIRMED": "解除暂停",
    "REVIEW_SCHEDULED": "安排复核",
    "FOLLOWUP_DUE": "复核到期",
    "REVIEW_SIGNED": "复核结论",
    "BASELINE_RECORDED": "记录训练基线",
    "SUMMARY_INGESTED": "接收训练摘要",
    "SUMMARY_QUARANTINED": "隔离冲突摘要",
}


def render_patient_view(facts: Mapping[str, Any]) -> dict[str, Any]:
    """把服务侧事实渲染为患者可读的简明状态。"""
    consent = facts.get("consent_status", "missing")
    if consent == "withdrawn":
        authorization = "已撤回"
    elif consent == "active":
        authorization = "有效"
    else:
        authorization = "未记录"

    pause = facts.get("active_pause")
    overdue = facts.get("overdue_review")
    if consent == "withdrawn":
        training_status = "已停止（授权已撤回）"
    elif pause:
        training_status = "已暂停"
    elif overdue:
        training_status = "待复核"
    elif consent == "active":
        training_status = "可训练"
    else:
        training_status = "未开始"

    notices: list[str] = []
    if consent == "withdrawn":
        notices.append("授权已撤回：不再安排新训练，历史责任记录依法保留")
    if pause:
        notices.append(f"临床暂停中：{pause.get('reason')}（须由另一名有资质人员确认解除）")
    if overdue:
        notices.append(f"复核已到期（{overdue.get('due_at')}），复核完成前暂停放行")

    last = facts.get("last_decision")
    return {
        "patient_id": facts.get("patient_id"),
        "authorization": authorization,
        "training_status": training_status,
        "active_pause_reason": pause.get("reason") if pause else None,
        "next_review_at": facts.get("next_review_at"),
        "last_decision": (
            {
                "result": "放行" if last.get("decision") == "released" else "暂停",
                "at": last.get("occurred_at"),
                "reasons": [
                    gate.get("message") for gate in last.get("gates", []) if not gate.get("passed")
                ],
            }
            if last
            else None
        ),
        "notices": notices,
    }


def filter_history(
    entries: Iterable[Mapping[str, Any]],
    *,
    consent_withdrawn: bool,
    purpose: str,
) -> list[dict[str, Any]]:
    """按撤回状态与查看目的过滤历史记录；任何情况下都不删除记录。"""
    visible: list[dict[str, Any]] = []
    for entry in entries:
        event_type = entry.get("event_type", "")
        category = EVENT_CATEGORIES.get(event_type, "accountability")
        if consent_withdrawn and category == "clinical_detail" and purpose != "legal":
            continue
        visible.append(
            {
                "event_id": entry.get("event_id"),
                "event_type": event_type,
                "label": EVENT_LABELS.get(event_type, event_type),
                "category": category,
                "occurred_at": entry.get("occurred_at"),
                "summary": entry.get("summary"),
            }
        )
    return visible
