# 领域约定

脑机接口创新场景大赛展示脑控轮椅和机械臂；报道同时指出非侵入式信号存在噪声和稳定性挑战、真实场景安全窗口短，产品多数仍处临床试验阶段。

本仓库分两层：

- **领域契约**（`contracts/domain.schema.json`）：可稳定交换的事件信封与载荷约定。所有发生时间必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。
- **训练决策服务**（`src/neuro_rehab/service.py`）：只管理评估与授权，不接入硬件，不替代医疗判断。相同事件标识的业务幂等、冲突隔离和状态推进由本层负责。

## 聚合与事件

聚合类型：`patient_consent`、`device_profile`、`staff_profile`、`venue_profile`、`training_case`、`safety_review`。

事件类型按主题分组：

- 授权：`CONSENT_RECORDED`、`CONSENT_WITHDRAWN`
- 设备：`PROFILE_APPROVED`、`DEVICE_STATUS_REPORTED`
- 人员与场地：`STAFF_REGISTERED`、`VENUE_REGISTERED`、`VENUE_CONDITION_RECORDED`
- 训练：`BASELINE_RECORDED`、`BOOKING_PLACED`、`THERAPIST_APPROVAL_RECORDED`、`SESSION_DECIDED`、`SESSION_HELD`
- 临床暂停：`PAUSE_RECORDED`、`RESUME_CONFIRMED`
- 训练摘要：`SUMMARY_INGESTED`、`SUMMARY_QUARANTINED`
- 复核：`REVIEW_SCHEDULED`、`FOLLOWUP_DUE`、`REVIEW_SIGNED`

## 事件载荷

各事件的必填载荷字段见 `contracts/domain.schema.json` 的 `payload_required_by_event`，例如：

- `PROFILE_APPROVED`：`device_id`, `device_revision`, `decoder_version`, `reviewer_id`
- `SESSION_DECIDED`：`booking_id`, `patient_id`, `decision`, `gates`（全部门禁结果与版本快照）
- `SESSION_HELD`：`booking_id`, `reason`, `observed_at`
- `REVIEW_SIGNED`：`review_id`, `decision`, `reviewer_id`
- `SUMMARY_QUARANTINED`：`session_id`, `summary_version`, `fingerprint`, `conflict_with`

## 服务层规则

- **职责分离**：治疗师可暂停训练；设备维护人员只能报告设备状态；解除临床暂停必须由另一名有资质人员确认。
- **版本不可追改**：事件账仅追加，设备或算法版本更新只影响新评估；`decision_audit` 可对比决定时与当前的版本快照。
- **授权撤回**：停止新训练并限制后续查看（临床细节仅法律目的可见），既有责任记录依法保留，任何记录都不删除。
- **摘要幂等**：同一训练摘要按内容指纹幂等；版本相同但内容变化的摘要隔离待核对。
- **容量**：预约并发不得超过治疗师与场地容量，检查与入账在同一锁内完成。
- **可解释**：每次评估记录全部门禁结果，`explain_decision` 说明某次训练为何放行或暂停。
- **恢复**：服务重启重放事件账恢复状态，`recover` 为到期未复核记录补发 `FOLLOWUP_DUE`（幂等，不重复）。
