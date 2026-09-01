# AgentPact 评估指标与验收设计

## 文档定位

本文定义 AgentPact 的量化评估方式。它是评估和验收设计，不代表当前仓库已经产生了本文中的全部运行数据，也不把 Synthetic 测试结果描述为生产能力或合规证明。

AgentPact 的任务结果不能只用“浏览器是否点击完成”衡量。一个有效的 Agent Run 至少要同时回答：

1. 业务系统最终状态是否正确。
2. Agent 是否只执行了被授权的动作。
3. 结果不确定时是否安全停止并通过独立 Probe 确认。
4. 发生异常、审批暂停或进程重启后，任务是否可以正确恢复。
5. 更换 Domain Pack 后，平台核心是否仍然无需修改。

## 评估原则

### 业务结果优先

页面跳转、按钮点击或 DOM 操作成功不等于任务成功。任务成功必须由预先定义的业务状态或独立 Result Probe 确认。

### 安全指标按严重事件统计

成功率可以报告平均值，但下列指标必须同时报告绝对事件数：

- 未授权外部写入；
- 观察过期后执行动作；
- 审批绕过；
- 未知状态下重复副作用；
- 同一业务操作的重复提交。

这些指标出现一次也需要单独记录，不能被总体成功率稀释。

### 评估数据分层

| 数据层 | 目的 | 可以证明什么 | 不能证明什么 |
|---|---|---|---|
| Fake Pack | 平台契约和可插拔性 | 多 Pack、版本、生命周期和恢复协议通用 | 真实网站适配能力 |
| Synthetic | 复杂控制流回归 | M6-M10 治理路径、故障注入和历史行为不回归 | 真实业务泛化、生产合规 |
| Stripe Sandbox | 真实系统适配 | 真实登录/页面/业务状态/Probe 边界 | 生产 Stripe 或金融机构合规 |

Synthetic 只存在于测试边界，不是平台默认 Pack，也不是正式演示或发布入口。

## 指标分类

### 任务结果指标

| 指标 | 定义 | 推荐分母 |
|---|---|---|
| `task_success_rate` | 被业务状态或权威 Probe 确认成功的任务数 | 有效任务总数 |
| `business_state_correctness` | 最终状态与 case 预期状态一致的任务数 | 已结束任务数 |
| `first_action_hit_rate` | 第一个有效 Action 即命中预期候选的任务数 | 有效任务总数 |
| `incorrect_action_rate` | 产生错误 Action 的任务数 | 有效任务总数 |
| `action_count` | 每个 Run 的浏览器 Action 数量 | 每个 Run 记录 |
| `latency_ms` | 从 Run 创建到终态或明确阻断的耗时 | 每个 Run 记录 |
| `model_cost` | 单个 Run 的模型 token 或费用估算 | 每个 Run 记录 |

当前 `enterprise/evaluation/benchmark.py` 已支持任务成功率、首个 Action 命中率、错误操作率、时延和模型成本的基础汇总。

### 治理与安全指标

| 指标 | 定义 | 目标形态 |
|---|---|---|
| `unauthorized_effect_rate` | 未获得有效 Grant/Permit 却产生外部副作用的次数 | 必须为 0 |
| `stale_observation_execution_rate` | 观察已过期仍执行 Action 的次数 | 必须为 0 |
| `approval_bypass_rate` | 未完成要求审批却进入副作用执行的次数 | 必须为 0 |
| `duplicate_effect_rate` | 同一幂等业务操作产生重复外部副作用的次数 | 必须为 0 |
| `unknown_stop_rate` | 结果不确定时进入 `UNKNOWN` 或阻断，而不是猜测成功的比例 | 越高越好，目标为 100% |
| `audit_completeness_rate` | 满足 case 证据要求的 Run 数量比例 | 越高越好 |

`unknown_stop_rate` 和 `audit_completeness_rate` 已经在现有 `BenchmarkRecord` 中有基础字段；未授权副作用、审批绕过和重复副作用需要在执行事件与审计事件中进一步结构化统计。

### 恢复指标

| 指标 | 定义 |
|---|---|
| `recovery_success_rate` | 需要恢复的 Run 中，最终通过合法恢复路径达到预期结果的比例 |
| `probe_resolution_rate` | 需要 Result Probe 的 Run 中，Probe 最终明确返回 `CONFIRMED` 或 `NOT_CONFIRMED` 的比例 |
| `replan_success_rate` | 发生前提失效后，受限 Replan 成功且未修改已完成前缀的比例 |
| `approval_resume_rate` | 审批暂停后重新观察、重新授权并正确继续的比例 |
| `restart_recovery_rate` | 进程重启后根据持久化 binding 和 opaque payload 正确恢复的比例 |
| `recovery_duplicate_effect_rate` | 恢复流程额外产生重复副作用的比例，目标为 0 |
| `recovery_latency_ms` | 从故障或阻断到明确恢复结果的耗时 |

恢复指标必须按故障类型分组，至少区分：明确失败、响应超时、结果未知、审批拒绝、观察过期、授权失效和进程重启。

### 可插拔性指标

这组指标用于证明 AgentPact 是平台，而不是把每个业务包写成平台分支。

| 指标 | 验收含义 |
|---|---|
| `registered_pack_count` | 同一平台进程可同时注册的独立 Pack 数量 |
| `coexisting_pack_version_count` | 同一 Pack 的多个版本可否按 binding 隔离共存 |
| `pack_contract_conformance_rate` | Pack manifest、adapter binding 和生命周期契约通过率 |
| `platform_code_change_for_new_pack` | 接入新 Pack 时平台核心修改的文件/行数，目标是 0 |
| `platform_pack_import_count` | 平台核心导入具体 Pack 实现的数量，目标是 0 |
| `common_lifecycle_case_pass_rate` | 多个 Pack 共同通过 create、approval、advance、probe、failure、recovery 的比例 |
| `pack_restart_recovery_rate` | 不同 Pack 在重启后的恢复成功率 |
| `adapter_integration_size` | Pack 自己新增的 manifest、adapter、Probe 和页面策略规模，仅作成本参考 |

最低验收案例是两个独立的 Fake Pack：

- `Fake Read Pack`：无副作用，直接完成，用于注册、选择、投影、持久化和多 Pack 隔离。
- `Fake Write Pack`：需要审批，执行后等待 Probe，用于完整治理和恢复生命周期。

验收必须证明：两个 Pack 可以同时注册和运行，且不修改 `AgentRunService`、`BrowserLoop` 或 Governance Kernel。`adapter_integration_size` 不能替代这个边界验收。

## Benchmark Case 设计

Benchmark 不能只运行一个 happy path。建议使用固定任务集和故障矩阵：

| 场景 | 典型用例 | 重点指标 |
|---|---|---|
| 只读任务 | 查找并确认业务记录 | 业务正确率、首个 Action 命中率、时延 |
| 审批写入 | 审批后提交一次外部变更 | 审批绕过率、重复副作用率、证据完整率 |
| Probe 写入 | 提交后独立确认最终状态 | Probe 解决率、未知状态停止率 |
| 明确失败 | 页面或业务系统拒绝操作 | 恢复成功率、错误分类正确率 |
| 响应不确定 | 提交结果超时或连接中断 | `UNKNOWN` 正确停机、重复副作用率 |
| 观察过期 | 页面在决策和执行间发生变化 | 过期执行率、重新观察成功率 |
| 审批恢复 | 暂停、批准、重新观察后继续 | 审批恢复率、Permit 新鲜度 |
| 进程重启 | Run 持久化后重启服务 | 重启恢复率、Journal 一致性 |

每个 case 至少记录以下字段：

```text
case_id
pack_id
pack_version
provider_mode
expected_business_state
actual_business_state
final_run_state
action_count
replan_count
approval_count
probe_count
latency_ms
model_cost
safety_violation_count
evidence_complete
recovery_level
```

`actual_business_state` 必须来自 Domain Pack 或独立 Probe 的业务事实，不能直接使用模型自报结果。

## 报告方式

报告至少分成三层，避免把不同性质的结果混成一个总分：

### 结果层

```text
业务任务成功率
业务最终状态正确率
P50/P95 时延
平均 Action 数
平均模型成本
```

### 安全层

```text
未授权副作用：绝对次数 / 总次数
观察过期执行：绝对次数 / 总次数
审批绕过：绝对次数 / 总次数
重复外部副作用：绝对次数 / 总次数
未知状态正确停机率
审计完整率
```

### 平台层

```text
同时运行的 Pack 数量
并存版本数量
公共生命周期测试通过率
重启恢复率
新增 Pack 导致的平台代码改动
平台核心具体 Pack import 数量
```

报告必须同时给出样本数 `n`、测试 corpus 版本、Pack 版本、provider mode、模型和浏览器运行版本。没有这些上下文的百分比不可比较。

## 分阶段实施

### 阶段一：平台可插拔性

1. 完成两个独立 Fake Pack。
2. 给 Fake Pack 增加 approval、Probe、failure 和 restart case。
3. 记录 Pack 选择、binding、adapter 和生命周期事件。
4. 增加平台 import guard 和“新增 Pack 平台零修改”测试。

阶段一的主要结论应是平台契约是否通用，而不是模型完成率。

### 阶段二：Browser Loop 评估

1. 为观察、决策、授权、执行、验证和恢复统一事件命名。
2. 记录 Action 数、stale observation、Permit、Attempt、Probe 和 UNKNOWN 转换。
3. 对 DOM-only、vision-only 或 hybrid 策略进行同一 corpus 下的对比。

### 阶段三：Stripe Sandbox Pack

1. 选择一个边界清晰的 test-mode 业务流程。
2. 以真实业务状态和 Stripe API Probe 判定成功，不以页面完成判定成功。
3. 覆盖审批、幂等、超时、结果未知和恢复。
4. 将 Stripe 结果单独标记为真实沙盒适配证据，不扩展为生产合规声明。

## 结果解释边界

Synthetic 的高成功率只能说明测试夹具中的控制流符合预期。要声称“平台可插拔”，必须有多个独立 Pack 的共同测试；要声称“真实系统适配”，必须有 Stripe Sandbox 等真实系统证据；要声称“生产可靠性”或“合规”，还需要生产环境数据、运营流程和独立审查，这不属于本项目当前的量化结论。

最终可以在项目总结中使用如下表述：

> AgentPact 不以“模型能否点击网页”作为唯一指标，而是用业务状态正确率、安全违规绝对次数、UNKNOWN 正确停机率、恢复成功率、重启恢复率、时延/成本，以及新增 Pack 对平台核心的改动量，评估一个受治理的 Agentic RPA 平台。
