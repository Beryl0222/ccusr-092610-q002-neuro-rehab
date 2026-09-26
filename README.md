# 脑控康复训练决策服务

面向康复科脑控轮椅试用场景的训练决策服务：把患者授权、设备与解码器版本、个体训练基线、场地条件、治疗师批准、训练摘要、暂停原因和复核结论连成可追溯记录。服务只管理评估与授权，不接入硬件，不替代医疗判断。

## 目录

- `contracts/domain.schema.json`：事件信封、事件类型与各事件必填载荷约定。
- `src/neuro_rehab/contracts.py`：契约校验（时间、版本、枚举、载荷）。
- `src/neuro_rehab/service.py`：训练决策服务（评估、授权、暂停、复核、幂等与隔离）。
- `src/neuro_rehab/store.py`：仅追加事件账（JSONL 持久化 + 重放恢复）。
- `src/neuro_rehab/decisions.py`：放行/暂停门禁结果与结构化中文解释。
- `src/neuro_rehab/views.py`：患者简明视图与撤回后的查看限制。
- `src/neuro_rehab/actors.py` / `errors.py`：角色权限与错误类型。
- `src/neuro_rehab/cli.py`：命令行校验单个事件。
- `src/neuro_rehab/demo.py`：端到端演示。
- `tests/`：契约测试与服务测试（权限、门禁、幂等、容量、恢复、患者视图）。
- `docs/domain.md`：领域对象、事件语义与服务层规则。

## 快速开始

```bash
# 运行全部测试
python3 -m unittest discover -s tests

# 端到端演示（评估→暂停→解除→摘要幂等→撤回→重启恢复）
PYTHONPATH=src python3 -m neuro_rehab.demo

# 校验单个事件样例
PYTHONPATH=src python3 -m neuro_rehab.cli contracts/domain.schema.json data/sample.json
```

## 服务用法

```python
from neuro_rehab import Actor, Role, TrainingDecisionService

svc = TrainingDecisionService("events.jsonl")          # 事件账路径；缺省为内存
op = Actor("op-1", frozenset({Role.OPERATOR}))
svc.register_staff(op, "th-1", ["therapist"], "2026-09-26T08:00:00+08:00")
# ... 登记授权、设备版本、基线、场地、预约、治疗师批准 ...
result = svc.evaluate_booking(actor, "bk-1", "2026-09-26T10:00:00+08:00")
print(result["decision"])                              # released / paused
print(result["explanation"]["summary"])                # 为何放行或暂停
```

关键规则见 `docs/domain.md`：职责分离（维护人员只能报告状态、解除暂停须另一名有资质人员确认）、版本不可追改、授权撤回后限制查看但保留责任记录、摘要按指纹幂等且冲突隔离、预约不超容量、服务恢复后继续到期复核。
