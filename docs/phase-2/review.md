# Phase 2 Architecture Review: Action Governance Bridge

## 结论

**接受但需修改。**

提案正确地把问题收敛为单 Agent Harness：Skyvern 继续负责感知、规划与浏览器执行，业务控制面在不可绕过的执行边界裁决 Action。`TaskContract -> ActionIntent -> PolicyDecision -> ExecutionPermit -> PendingAction` 的方向成立，且“审批后重新感知、重新分析、重新签发 permit”是正确的安全原则。

但当前版本不能直接进入 `enforce` 实现。它尚未定义 Skyvern 现有任务/工作流如何持久化暂停和恢复，也未封住全部浏览器执行旁路；一次性 permit 与浏览器外部副作用之间的崩溃一致性也没有可实施协议。建议只在补齐下列“必须修改”项后进入 Phase 2.0/2.1；Phase 2.2 与 2.3 不应并行启动。

## 与现有代码不兼容

### 必须修改

1. **`PENDING_APPROVAL` 没有接入 Skyvern 的任务与工作流状态机。**
   提案要求任务进入 `PENDING_APPROVAL`，但现有 `TaskStatus` 只有 `created/queued/running` 和终态，没有该状态，也没有从该状态恢复到 `running` 的迁移（[tasks.py](E:/meizhouyu/agentstudy/_tmp_finrpa_enterprise/skyvern/forge/sdk/schemas/tasks.py:186)）。工作流层目前假设任务最终会映射为 block 的 completed/failed/canceled 等终态。必须明确选择并实现一种模型：
   - 扩展 Skyvern `TaskStatus`、数据库 enum/约束、workflow block 状态与调度器，支持真正的挂起与恢复；或
   - 保持 Skyvern Task 只表示浏览器执行，新增持久化 `GovernanceRun`/`PendingAction` 状态机，由恢复调度器创建新的可运行 step。

   不能只新增 `PendingAction` 表后把原 Task 留在 `running`；worker 崩溃、工作流等待和恢复归属都会变成未定义行为。

2. **“在 `ActionHandler` 公共入口验 permit”不能满足不可绕过承诺。**
   主循环确实通过 `ActionHandler.handle_action()` 执行（[agent.py](E:/meizhouyu/agentstudy/_tmp_finrpa_enterprise/skyvern/forge/agent.py:1378)，[handler.py](E:/meizhouyu/agentstudy/_tmp_finrpa_enterprise/skyvern/webeye/actions/handler.py:391)），但脚本生成路径直接调用 `handle_click_action`、`handle_input_text_action` 等具体 handler，甚至保留 locator fallback（[real_skyvern_page_ai.py](E:/meizhouyu/agentstudy/_tmp_finrpa_enterprise/skyvern/core/script_generations/real_skyvern_page_ai.py:204)）。

   必须在提案中列出所有执行入口，并选择：禁止这些路径用于受治理任务、让它们全部通过同一个 Governor、或把低层 handler 变为私有且只允许经统一执行器调用。否则“没有 permit 不产生外部副作用”的验收标准不成立。

3. **多 Action 批次与快照绑定 permit 存在天然冲突。**
   `agent_step` 从同一 `ScrapedPage` 解析一批 actions 后顺序执行（[agent.py](E:/meizhouyu/agentstudy/_tmp_finrpa_enterprise/skyvern/forge/agent.py:1179)，[agent.py](E:/meizhouyu/agentstudy/_tmp_finrpa_enterprise/skyvern/forge/agent.py:1242)）。提案却要求 permit 绑定 `snapshot_hash`。第一条会改变 DOM 的 action 执行后，后续 action 的旧快照通常已失效。

   必须规定批次语义：默认只授权并执行一个状态改变 action，随后重新抓取、重新分析、重新授权；只有经过声明的只读或原子链才可共享 observation。该规则也必须覆盖 auto-complete、点击触发导航、下载、新窗口和 CUA 坐标操作。

4. **permit 消费与浏览器副作用之间没有崩溃一致性协议。**
   `used_at` 只能表达 permit 被消费，无法回答“消费后浏览器 click 已生效但进程崩溃”还是“尚未发送 click”。这不是可用数据库事务跨越的边界。

   必须将高影响执行建模为持久化状态：`AUTHORIZED -> EXECUTING -> CONFIRMED | UNKNOWN | FAILED`，在调用 Playwright 前持久化 `EXECUTING` 并加锁；重启后先做业务结果探测。`UNKNOWN` 必须转人工或重新核验，不能自动重试。提案中的“幂等键或结果探测”需要提升为明确协议和表字段，而非实现备注。

5. **TaskContract 的创建、身份快照与再授权来源没有定义。**
   当前 `TenantContext` 是请求期 `ContextVar`，请求结束即被重置（[context.py](E:/meizhouyu/agentstudy/_tmp_finrpa_enterprise/enterprise/tenant/context.py:13)）；异步 task、审批恢复 worker 不会天然拥有 `UserContext`。而 `TaskExtensionModel` 只记录部门、业务线、风险和 `created_by`，不保存完整授权快照（[models.py](E:/meizhouyu/agentstudy/_tmp_finrpa_enterprise/enterprise/auth/models.py:234)。

   必须指定 native Task、workflow Task、模板 Task 分别在哪个创建入口生成 TaskContract；持久化 initiator、服务主体、授权范围快照、策略版本、契约版本和过期规则。审批时还必须重新校验审批人权限，并明确策略是采用“任务创建时权限”还是“执行/恢复时权限”。

6. **高影响语义仍然过于松散，无法可靠约束价值流转。**
   `ActionIntent.target`、`extracted_facts`、`expected_outcome` 都是无类型 `dict`。对于 `payment/submit/approve/delete`，必须有可比较的 canonical fields，例如业务主体 ID、对象版本、金额/币种、收款方、提交原因、外部系统标识和 commit preconditions。坐标型 CUA action 没有 element id，也必须将坐标、截图尺寸、目标文本/视觉证据纳入受限策略；无法提取时默认不能自动跨越 commit boundary。

7. **敏感数据与模型出境边界不够具体。**
   提案要求脱敏，但现有 `hash_raw_value()` 使用无密钥 SHA-256（[sanitizer.py](E:/meizhouyu/agentstudy/_tmp_finrpa_enterprise/enterprise/audit/sanitizer.py:116)），对低熵手机号、证件号和金额组合可被离线猜测。更重要的是，现有 Skyvern 会将 DOM 与截图交给模型；提案没有定义哪些字段可传给哪类模型、是否允许外部模型、截图如何遮罩、原始证据的保留期与访问审计。

   必须引入字段级 data classification、模型/地域 allowlist、截图/DOM 预处理、HMAC fingerprint（密钥轮换）和最小保留期。金融场景下，“对象存储在内网”不能替代“发送给 LLM 前的出境控制”。

8. **审批数据模型不足以承载职责分离和并发决策。**
   当前 `ApprovalRequestModel` 没有 requester/principal、关联 intent/permit、版本号或幂等键（[models.py](E:/meizhouyu/agentstudy/_tmp_finrpa_enterprise/enterprise/approval/models.py:48)）；现有 API 也不是数据库 CAS 更新。必须在新模型中加入请求人、contract/intent/pending-action 外键、`row_version` 或条件更新、撤销原因、审批快照和唯一约束，并在数据库事务内强制“发起人不得审批自己”。

9. **现有 schema 迁移并不覆盖审批/审计/治理表。**
   当前 enterprise migration 仅能确认权限和 `task_extensions` 表；Phase 2 依赖的 approval、audit、contract、permit、pending-action、execution-attempt 表需要明确 Alembic migration、索引、外键、保留策略与升级顺序。不能依赖 `ensure_enterprise_schema.py` 或启动期 demo store。

### 建议修改

1. “不再使用固定 `sleep` 作为主等待策略”应改为渐进目标。现有 ActionHandler 与页面交互依赖等待配置和动画等待；Phase 2 先新增 `PageReadiness` 作为高影响动作的前置条件，不应在同一阶段替换全部等待行为。

2. `input`、`download`、`navigate` 不能按 action type 简单当成低风险。输入可触发即时提交，下载可能导出敏感数据，导航可能打开外部系统。策略应按实际 effect、页面语义、数据分级和契约判断，而不是靠动作名称白名单。

3. `CompleteAction` 需要单独治理。现有完成校验只验证浏览器导航目标（[handler.py](E:/meizhouyu/agentstudy/_tmp_finrpa_enterprise/skyvern/webeye/actions/handler.py:2139)）；TaskContract 的成功条件必须在任务完成前被显式评估，否则会出现“网页流程完成但业务事实未确认”。

4. `off/audit/enforce` 应定义故障语义：audit 写入失败是否降级、policy/permit 存储不可用时 enforce 是否 fail-closed、配置变更如何版本化和审计。开关应在 task/contract 创建时快照，避免一次任务中途改变裁决规则。

5. 增加针对 CUA、UI-TARS、脚本生成、缓存计划、重放/恢复、同一 step 多 action、浏览器自动跳转和执行中崩溃的回归用例；当前测试清单偏主路径。

### 可接受风险

1. Phase 2 不引入 Planner、多 Agent 或独立视觉服务是合理收敛；先建立单 Agent 的受治理执行边界，复杂编排可在稳定契约之上演进。

2. 采用 DOM 优先、视觉复核按需触发是合理的成本控制。前提是视觉复核的数据出境策略和失败时的 fail-safe 行为先被落实。

3. 先以 `audit` 模式收集真实误报，再按机构/工作流灰度进入 `enforce` 是正确的迁移方式。前提是 audit 事件能可靠关联到 action、contract、策略版本与证据引用。

## 两种已有审批等待思路的适用条件

现有代码中的 [create_approval_and_wait](E:/meizhouyu/agentstudy/_tmp_finrpa_enterprise/enterprise/approval/pubsub.py:178) 是“创建审批单后，由当前协程订阅 Redis 并等待”的同步等待思路；提案中的 `PendingAction + 持久化暂停 + 再感知恢复` 是第二种思路。

| 思路 | 适合条件 | 不适合条件 |
|---|---|---|
| 协程 + Redis Pub/Sub 同步等待 | 短时、单 worker、会话必须保持的交互，例如 OTP 或短时间人工确认；等待超时可直接失败且不涉及高价值外部提交。 | 金融审批、长时间人工等待、多 worker、服务重启、需要审计恢复或不可重复副作用。订阅丢失和进程重启会丢失运行现场。 |
| `PendingAction` 持久化暂停并重新调度 | 高风险提交、等待可能持续分钟/小时、审批后页面可能变化、需要跨 worker 恢复、需要完整审计与职责分离。 | 只为短时页面加载或验证码而使用会增加状态机和调度复杂度；这类场景保留现有短等待更合适。 |

两者不应混用为同一高风险动作的双重事实来源：数据库状态必须是审批与恢复的唯一事实来源，Redis 只负责通知。

## 对两层接点的审查结论

提案中的 `parse_actions -> ActionGovernor.authorize -> ActionHandler.handle_action(permit)` 不是两种可替代架构，而是两个必须协同的层次：

- 前置 Governor 负责解释意图、生成审计、给出 allow/approval/deny 决策；
- 末端 ActionHandler 负责验证 permit 并阻止普通主路径绕过。

但它们仍不足以覆盖直接调用具体 handler 或 locator 的路径，因此必须先完成执行入口盘点与封口，才能宣称“唯一执行边界”。

## 需要补充的验收门槛

- 故障注入：分别在 permit 签发后、`EXECUTING` 落库后、浏览器 click 发出后、结果校验前杀死 worker；每种情况都不得自动重复提交。
- 并发测试：两个审批人、两个恢复 worker、审批撤销与超时竞争、同一任务并发执行。
- 身份测试：JWT 过期、用户离职/权限收回、审批人权限变更、服务账号执行与用户发起人不一致。
- 数据测试：截图、DOM、prompt、指纹、审计事件中出现手机号、账号、密码、OTP、身份证和金额组合时的脱敏/阻断策略。
- 批处理测试：同一 LLM 响应含多个动作时，首个 DOM 修改后后续 permit 必须按新 observation 重新授权或被安全终止。

## 最终裁决

**接受但需修改。**

可以接受 Action Governance Bridge 作为 Phase 2 的架构方向，并允许先实施 Phase 2.0 的契约、schema、审计基线与 audit-only 观测；但在完成“任务暂停恢复模型、所有执行旁路封口、外部副作用的 UNKNOWN 状态协议、持久化身份/职责分离、数据出境策略”之前，不接受进入 `enforce`、审批恢复或高风险自动执行实现。

---

## Phase 2 实施复审（2026-07-19）

### 复审范围

本节复审原 `proposal.md` 中的治理底座实施情况，并与后续的 [next-stage-proposal.md](next-stage-proposal.md) 对齐。它不代表真实 enforce 已可启用。

### 已完成并可采纳的实现

1. **真实运行保持 audit-only。**
   `ForgeAgent` 已在 Action 解析后记录治理候选；`GOVERNANCE_MODE=enforce` 被配置层主动拒绝，避免在 permit 主链未接通时制造假安全感。

2. **敏感输入审计已具备基础防护。**
   候选 Action 审计会脱敏 InputText 等输入，治理记录不应直接保存原始敏感值；模型出境 allowlist 已有配置入口。字段级数据分类、截图遮罩、模型/地域策略和保留期仍是下一阶段硬门槛。

3. **UNKNOWN 状态协议已具备持久化底座。**
   `ExecutionAttempt` 已覆盖 `AUTHORIZED -> EXECUTING -> CONFIRMED | FAILED | UNKNOWN`；UNKNOWN 只能通过带结果探测证据的解析流程收敛，不能自动重放旧 Action。该状态机尚未由真实 Skyvern enforce 主链驱动。

4. **长等待审批采用持久化暂停/恢复。**
   `PendingAction`、数据库审批裁决、申请人不可自审、CAS、`PENDING_APPROVAL -> RESUMING` 和恢复后重新感知均已具备基础实现。Redis 只用于通知，数据库是审批事实来源。真实业务工作流中的调度归属仍需在任务 1、2、8 中统一接线。

5. **身份与审批关联已有基础。**
   `ApprovalRequestModel` 已有 requester 字段；TaskContract 能从 TaskExtension 获取发起人、部门、业务线等快照。仍需定义 native Task、workflow Task、模板 Task 的统一创建入口与执行/恢复时再授权规则。

6. **迁移已覆盖治理基线。**
   Contract、permit、持久化审批、requester 等迁移已存在。部署需要处理 core 与 enterprise 的 Alembic 双 head，使用 `alembic upgrade heads`，并在目标 PostgreSQL 环境演练升级与回滚。

### 尚未完成、不得宣称完成的项目

1. `Governor -> Permit -> ExecutionAuthorization -> ActionHandler` 尚未接入 ForgeAgent 的真实执行主链。
2. 所有浏览器副作用入口尚未完成盘点与封口；脚本生成、具体 handler、locator fallback、CUA/UI-TARS、缓存和推测动作都必须纳入 ExecutionProfile 策略。
3. `payment`、`submit`、`approve`、`delete` 等高影响业务尚未定义 canonical facts、结果探测器和可审计的 commit 前置条件。
4. 真实业务 Work Order、受限 Planner、Capability Grant 与 L0--L4 恢复路由尚未实现。
5. DOM、截图、Prompt 的字段级出境控制与可重复的多模态/故障注入评测尚未完成。

### 复审后的实施约束

- `input`、`download`、`navigate` 不能仅按技术 Action 类型判为低风险；必须结合 effect、数据分级、页面语义和 Contract。
- 页面等待是渐进优化目标。现有动画等待、超时和页面重抓可继续存在；高风险动作新增 `PageReadiness` 前置校验，而不是一次性移除全部等待。
- `CompleteAction` 必须在未来由 Domain Pack 的 `BusinessResultProbe` 校验业务成功条件；网页导航目标完成不等于业务完成。
- 默认一个 Observation 只允许一个可能改变状态的 Action；随后重新感知、重新分析并重新授权。
- enforce 下治理持久化、策略或 permit 服务不可用时必须 fail-closed；audit 写入失败必须可观测、可重试。

### 最终 Decision

**接受“Phase 2 下一阶段：受控编排与韧性受治理执行”作为唯一推荐架构和后续实施路线。**

既有 Phase 2 治理底座予以保留，不回退、不另建第二浏览器执行器；Skyvern 保持唯一浏览器执行器。下一阶段按 [next-stage-proposal.md](next-stage-proposal.md) 的八项任务及依赖顺序推进：

```text
能力与授权 -> Plan/Work Order -> L0--L4 与证据模型
-> fallback 策略与评测 -> 最小 Domain Pack -> 真实 enforce
```

**当前批准范围只包括 Proposal 中任务 1--6 的设计和基础实现准备。**

任务 7 需要业务方确认最小领域包及 canonical facts；任务 8 的真实 enforce 只有在 Proposal 第 9 节全部门槛满足、并再次取得批准后才可启动。此前系统必须维持 `off` 或 `audit`，不得将任何现有 permit、审批或 recovery 底座表述为已经提供生产级高风险自动执行。

