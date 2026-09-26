# 脑控康复安全决策账

脑机接口创新场景大赛展示脑控轮椅和机械臂；报道同时指出非侵入式信号存在噪声和稳定性挑战、真实场景安全窗口短，产品多数仍处临床试验阶段。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/neuro_rehab/contracts.py`：基础契约校验。
- `src/neuro_rehab/domain.py`：角色、决策与理由对象。
- `src/neuro_rehab/ledger.py`：追加式事件账，按事件标识幂等，可落盘恢复。
- `src/neuro_rehab/service.py`：训练决策服务（评估与授权，不接入硬件）。
- `src/neuro_rehab/cli.py`：命令行校验入口。
- `tests/`：契约测试与服务层规则测试。
- `docs/domain.md`：领域对象、事件语义与服务层规则。

## 训练决策服务

`TrainingDecisionService` 把患者同意、设备与解码器版本、个体训练基线、场地条件、治疗师批准、信号质量摘要、暂停原因和复训结论连成可追溯记录：

- 角色分离：治疗师评估与暂停，设备维护人员只上报状态，解除临床暂停须另一名有资质人员确认。
- 版本固化：案例开立时固化设备/解码器/基线版本，版本更新不追改既往结论。
- 授权撤回：停止新训练并限制后续查看，既有责任记录依法保留。
- 摘要幂等与隔离：重复上传按指纹去重，同版本内容变化即隔离。
- 容量约束：并发预约不超过治疗师与场地容量。
- 可解释：每次放行或暂停都给出理由，`explain_session` 输出中文说明；放行的训练到期复核，服务恢复后继续。

```python
import json
from pathlib import Path
from neuro_rehab import Actor, EventLedger, Role, TrainingDecisionService

schema = json.loads(Path("contracts/domain.schema.json").read_text(encoding="utf-8"))
service = TrainingDecisionService(EventLedger(schema, "ledger.jsonl"))
service.register_staff("t-01", Role.THERAPIST, "2026-09-26T09:00:00+08:00")
# ...登记场地、记录同意、上报并批准设备、设定基线、开立案例、预约、上传摘要...
decision = service.evaluate_session(Actor("t-01", Role.THERAPIST), "s-01", "2026-09-26T10:00:00+08:00")
print(service.explain_session("s-01"))
```

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m neuro_rehab.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。
