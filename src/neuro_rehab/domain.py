"""训练决策服务的领域对象。

服务只管理评估与授权：不接入硬件，也不替代医疗判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    """系统内的人员角色。"""

    THERAPIST = "therapist"  # 治疗师：设定基线、预约、评估、暂停训练
    MAINTAINER = "maintainer"  # 设备维护人员：仅上报设备状态
    REVIEWER = "reviewer"  # 有资质复核人员：批准设备档案、确认复训、签署复核
    AUDITOR = "auditor"  # 法规审计：患者撤回授权后查看既有责任记录


class DecisionOutcome(str, Enum):
    """训练评估结论。"""

    RELEASED = "released"  # 放行
    PAUSED = "paused"  # 暂停


@dataclass(frozen=True)
class Actor:
    """一次操作的执行人；角色必须与人员名册登记一致。"""

    actor_id: str
    role: Role


@dataclass(frozen=True)
class Reason:
    """一条可解释的放行/暂停理由。"""

    code: str
    message: str


@dataclass(frozen=True)
class Decision:
    """一次训练的放行/暂停结论。

    设备修订、解码器版本与基线版本在决策时固化；
    后续设备或算法版本更新不会追改本结论。
    """

    session_id: str
    outcome: DecisionOutcome
    reasons: tuple[Reason, ...]
    device_revision: str
    decoder_version: str
    baseline_version: int
    decided_by: str
    decided_at: str

    def explain(self) -> str:
        """用中文说明本次训练为何放行或暂停。"""
        outcome = "放行" if self.outcome is DecisionOutcome.RELEASED else "暂停"
        lines = [
            f"训练 {self.session_id} 结论：{outcome}",
            f"依据版本：设备 {self.device_revision} / 解码器 {self.decoder_version} / 基线 v{self.baseline_version}",
            f"评估人：{self.decided_by}，评估时间：{self.decided_at}",
        ]
        lines.extend(f"- {reason.code}: {reason.message}" for reason in self.reasons)
        return "\n".join(lines)
