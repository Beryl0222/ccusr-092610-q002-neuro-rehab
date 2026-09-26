# 领域约定

脑机接口创新场景大赛展示脑控轮椅和机械臂；报道同时指出非侵入式信号存在噪声和稳定性挑战、真实场景安全窗口短，产品多数仍处临床试验阶段。

聚合对象包括`patient_consent`、`device_profile`、`training_case`、`safety_review`。事件类型包括`CONSENT_RECORDED`、`PROFILE_APPROVED`、`SESSION_HELD`、`REVIEW_SIGNED`、`FOLLOWUP_DUE`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `PROFILE_APPROVED`：载荷还需包含 `device_revision`, `reviewer_id`。
- `SESSION_HELD`：载荷还需包含 `reason`, `observed_at`。
- `REVIEW_SIGNED`：载荷还需包含 `decision`, `reviewer_id`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
