"""放行/暂停门禁评估结果与解释。

每次评估都把全部门禁结果连同当时的版本快照写入 ``SESSION_DECIDED``
事件，因此"某次训练为何放行或暂停"可以随时重放解释，且不受后续
设备或算法版本更新影响。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping

#: 门禁名称（稳定标识 → 展示名）。
GATE_LABELS = {
    "consent": "患者授权",
    "device": "设备与解码器版本",
    "baseline": "个体训练基线",
    "venue": "场地条件",
    "therapist_approval": "治疗师批准",
    "clinical_pause": "临床暂停",
    "review": "到期复核",
}

RELEASED = "released"
PAUSED = "paused"


@dataclass(frozen=True)
class GateResult:
    """单项门禁的评估结果。"""

    name: str
    passed: bool
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ok(name: str, message: str) -> GateResult:
    return GateResult(name=name, passed=True, code="ok", message=message)


def fail(name: str, code: str, message: str) -> GateResult:
    return GateResult(name=name, passed=False, code=code, message=message)


def decision_fingerprint(payload: Mapping[str, Any]) -> str:
    """决定幂等键：同一预约在相同版本快照下得到相同结论时不重复入账。"""
    material = {
        "booking_id": payload["booking_id"],
        "decision": payload["decision"],
        "gates": [(gate["name"], gate["code"]) for gate in payload["gates"]],
        "device_revision": payload.get("device_revision"),
        "decoder_version": payload.get("decoder_version"),
        "baseline_version": payload.get("baseline_version"),
        "consent_status": payload.get("consent_status"),
        "venue_condition": payload.get("venue_condition"),
    }
    blob = json.dumps(material, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def summary_fingerprint(content: Mapping[str, Any]) -> str:
    """训练摘要内容指纹：重复上传同一摘要按指纹幂等。"""
    blob = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def explain_decision(payload: Mapping[str, Any]) -> dict[str, Any]:
    """把 SESSION_DECIDED 载荷渲染为结构化中文解释。"""
    gates = list(payload.get("gates", []))
    failed = [gate for gate in gates if not gate.get("passed")]
    decision = payload.get("decision")
    if decision == RELEASED and not failed:
        summary = f"放行：{len(gates)} 项检查全部通过"
    else:
        summary = f"暂停：{len(failed)} 项检查未通过"
    return {
        "booking_id": payload.get("booking_id"),
        "patient_id": payload.get("patient_id"),
        "decision": decision,
        "decision_label": "放行" if decision == RELEASED else "暂停",
        "summary": summary,
        "failed_gates": [
            {
                "gate": gate.get("name"),
                "label": GATE_LABELS.get(gate.get("name"), gate.get("name")),
                "code": gate.get("code"),
                "message": gate.get("message"),
            }
            for gate in failed
        ],
        "passed_gates": [
            {
                "gate": gate.get("name"),
                "label": GATE_LABELS.get(gate.get("name"), gate.get("name")),
                "message": gate.get("message"),
            }
            for gate in gates
            if gate.get("passed")
        ],
        "versions": {
            "device_revision": payload.get("device_revision"),
            "decoder_version": payload.get("decoder_version"),
            "baseline_version": payload.get("baseline_version"),
            "venue_condition": payload.get("venue_condition"),
            "consent_status": payload.get("consent_status"),
        },
    }
