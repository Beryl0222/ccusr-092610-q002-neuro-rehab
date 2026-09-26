# 领域约定

脑机接口创新场景大赛展示脑控轮椅和机械臂；报道同时指出非侵入式信号存在噪声和稳定性挑战、真实场景安全窗口短，产品多数仍处临床试验阶段。

聚合对象包括`patient_consent`、`device_profile`、`training_baseline`、`training_case`、`training_session`、`venue_profile`、`staff_roster`、`safety_review`。事件类型包括`CONSENT_RECORDED`、`CONSENT_WITHDRAWN`、`STAFF_REGISTERED`、`VENUE_REGISTERED`、`DEVICE_STATUS_REPORTED`、`PROFILE_APPROVED`、`BASELINE_SET`、`CASE_OPENED`、`SESSION_BOOKED`、`SUMMARY_ACCEPTED`、`SUMMARY_QUARANTINED`、`DECISION_RECORDED`、`TRAINING_PAUSED`、`TRAINING_RESUMED`、`SESSION_HELD`、`FOLLOWUP_DUE`、`REVIEW_SIGNED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `CONSENT_WITHDRAWN`：载荷还需包含 `requested_by`。
- `STAFF_REGISTERED`：载荷还需包含 `role`。
- `VENUE_REGISTERED`：载荷还需包含 `capacity`, `condition`。
- `DEVICE_STATUS_REPORTED`：载荷还需包含 `device_revision`, `decoder_version`, `reporter_id`。
- `PROFILE_APPROVED`：载荷还需包含 `device_revision`, `reviewer_id`。
- `BASELINE_SET`：载荷还需包含 `baseline_version`, `therapist_id`。
- `CASE_OPENED`：载荷还需包含 `patient_id`, `device_revision`, `decoder_version`, `baseline_version`。
- `SESSION_BOOKED`：载荷还需包含 `case_id`, `therapist_id`, `venue_id`, `start`, `end`。
- `SUMMARY_ACCEPTED` / `SUMMARY_QUARANTINED`：载荷还需包含 `session_id`, `decoder_version`, `fingerprint`；隔离事件另含 `reason`。
- `DECISION_RECORDED`：载荷还需包含 `session_id`, `decision`, `evaluator_id`。
- `TRAINING_PAUSED`：载荷还需包含 `reason`, `paused_by`。
- `TRAINING_RESUMED`：载荷还需包含 `confirmed_by`, `conclusion`。
- `SESSION_HELD`：载荷还需包含 `reason`, `observed_at`。
- `FOLLOWUP_DUE`：载荷还需包含 `case_id`, `session_id`, `due_at`。
- `REVIEW_SIGNED`：载荷还需包含 `decision`, `reviewer_id`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。

## 服务层规则

`TrainingDecisionService` 只管理评估与授权，不接入硬件，也不替代医疗判断。所有状态变更先写入事件账再应用到内存状态，`restore` 可从账本文件恢复并继续履行到期复核。

### 角色

- 治疗师（`therapist`）：记录同意、设定个体基线、开立案例、预约、评估、暂停训练。
- 设备维护人员（`maintainer`）：只能上报设备与解码器版本状态，不能批准、暂停或评估。
- 有资质复核人员（`reviewer`）：批准设备档案、确认复训、签署到期复核。
- 法规审计（`auditor`）：患者撤回授权后查看既有责任记录。

### 状态推进

- 开立训练案例时固化当前获批的设备修订、解码器版本与最新基线版本；设备或算法版本更新只影响新案例，不追改既往训练结论。
- 治疗师可暂停训练并记录原因；解除临床暂停须由另一名有资质人员确认，并记录复训结论。
- 患者撤回授权后停止新训练、取消未开始的预约、限制后续查看；既有责任记录依法保留，仅法规审计可查。

### 幂等、隔离与容量

- 重复上传同一训练摘要按内容指纹幂等；同一训练同一解码器版本摘要内容变化时，新摘要被隔离，冲突澄清前该版本摘要不参与评估。
- 预约、评估、复核签署按业务标识幂等，重复提交不产生新记录。
- 并发预约不可超过治疗师个人容量与场地容量（按时段重叠判定）。

### 可解释与复核

- 每次评估给出放行或暂停结论及理由代码（如 `SIGNAL_BELOW_BASELINE`、`SUMMARY_QUARANTINED`），`explain_session` 输出中文说明。
- 放行的训练生成到期复核任务；服务恢复后 `due_followups` 继续返回到期未签署的复核。
- `patient_view` 向患者提供简明的授权与训练状态。
