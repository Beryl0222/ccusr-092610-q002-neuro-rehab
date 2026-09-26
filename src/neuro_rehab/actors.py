"""角色与操作者。

职责分离原则：
- 治疗师（therapist）可以批准与暂停训练；
- 设备维护人员（device_maintenance）只能报告设备状态；
- 解除临床暂停必须由另一名有资质人员（治疗师或复核员）确认。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    THERAPIST = "therapist"
    REVIEWER = "reviewer"
    DEVICE_MAINTENANCE = "device_maintenance"
    OPERATOR = "operator"
    PATIENT = "patient"


#: 有资质确认解除临床暂停的角色。
QUALIFIED_ROLES = frozenset({Role.THERAPIST, Role.REVIEWER})


@dataclass(frozen=True)
class Actor:
    """一次操作的执行者。"""

    actor_id: str
    roles: frozenset[Role] = field(default_factory=frozenset)

    def has_any(self, *roles: Role) -> bool:
        return any(role in self.roles for role in roles)

    @property
    def is_qualified(self) -> bool:
        return bool(self.roles & QUALIFIED_ROLES)


def parse_roles(values: list[str]) -> frozenset[Role]:
    """把登记载荷中的角色字符串解析为角色集合，未知角色报错。"""
    roles: set[Role] = set()
    for value in values:
        roles.add(Role(value))
    return frozenset(roles)
