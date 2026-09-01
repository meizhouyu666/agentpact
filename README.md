# AgentPact
> Governed Browser-Agent Harness, Domain Pack SDK & Conformance Kit

默认离线验证仅使用合成数据；显式 Stripe smoke 使用 test-mode 数据，不面向生产环境，也不包含部署。

AgentPact 是一个面向高风险浏览器任务的 Agentic RPA 执行与恢复框架。它关注的不只是 Agent 能否完成任务，还包括：Agent 是否有权执行、外部副作用是否经过确定性授权、结果不确定时能否安全停止，以及如何通过独立证据确认真实业务状态。

项目基于 [Skyvern](https://github.com/Skyvern-AI/skyvern) 的浏览器 Agent Loop 构建，在模型决策与 Playwright 副作用之间加入类型化领域契约、确定性授权、一次性执行许可、持久化尝试状态和分级恢复机制。当前包含 `synthetic.payment` 离线参考路径，以及显式手工触发的 `stripe.payment` Stripe test-mode hosted Checkout 路径；两者都不是生产系统，不包含部署。

## 为什么需要 AgentPact

传统浏览器自动化常把“点击成功”视为任务成功，但真实业务操作还必须回答：

- 自然语言目标是否对应当前用户已获授权的业务能力？
- 模型提出的计划是否超出 Capability、租户、数据范围或有效 Grant？
- 页面动作是否仍符合原始业务意图和最新页面证据？
- 提交后连接超时，操作究竟失败了，还是已经产生外部副作用？
- 恢复是在继续任务，还是会制造重复付款、重复订单或重复审批？

AgentPact 将这些问题放进浏览器执行主链的边界设计中。浏览器传输成功不等于业务成功；当结果无法确认时，系统进入 `UNKNOWN`，禁止重放，并且只能通过独立 Result Probe 重新观察目标系统。

> Agent 不应该在“不知道有没有成功”时再次点击提交。

## 架构

```mermaid
flowchart LR
    U[自然语言任务] --> I[身份、租户与可信业务上下文]
    I --> C[Capability / Domain Pack]
    C --> P[受约束 Planner / BusinessPlan]
    P --> W[ExecutionWorkOrder]
    W --> S[Skyvern Agent Loop]
    S --> G[Governance Kernel]
    G -->|ExecutionPermit| A[ActionHandler / Playwright]
    A --> R[Independent Result Probe]
    R --> D[确认、恢复或人工介入]
```

核心执行链为：

`Capability -> BusinessPlan -> ExecutionWorkOrder -> Action`

- **Domain Pack** 定义业务能力、输入槽、状态转换、风险、证据和 Result Probe，不直接操作 DOM 或 Playwright。
- **Planner** 只能在可信代码投影出的 Capability 和结构化约束内提出计划，不能创建身份、租户、Grant、策略、Permit、浏览器动作或业务值。
- **BusinessPlan** 表达受授权的业务步骤；可信编译器将其转换为顺序执行的 `ExecutionWorkOrder`。
- **ExecutionWorkOrder** 固定允许/禁止操作、成功标准、证据要求、恢复上限和原生 Task/Step 身份。
- **Skyvern Agent Loop** 负责 DOM、截图、页面内动作选择和浏览器级恢复；`ActionHandler` 是浏览器副作用的唯一执行入口。
- **Governance Kernel** 在副作用发生前完成策略、审批、最新观察、`ExecutionPermit` 和 `ExecutionAttempt` 裁决。
- **Result Probe** 独立确认最终业务结果。`UNKNOWN` 状态禁止动作重放和 Replan，直到精确关联的 Probe 给出权威结果。

## 核心设计

### Domain Pack SDK 与 Conformance Kit

Domain Pack SDK 使用版本化、类型化契约描述 Agent 可调用能力、业务事实、效果分类、状态机、证据要求和 Result Probe。离线 Contract Catalog 与活动运行时注册（Active Registry）严格隔离；运行时代码不能把离线 SDK 清单当作动态执行入口。

Conformance Kit 使用确定性检查验证 Pack 来源、所有者、版本、Capability、证据和安全语义。`synthetic.payment` 是离线参考 Pack；`stripe.payment` 另有真实 test-mode API Probe 和 hosted Checkout adapter，但仍不是生产安装。`DomainPackInstallation` 与 Active Registry 仍是后续接入边界。

### Stripe test-mode hosted Checkout
`app.py`/`store.py` 是仅供 recorded 的自建 loopback checkout；`live_browser.py` 才会调用 Stripe API、访问真实
`https://checkout.stripe.com/c/...`、使用 4242 测试卡，并由独立 `StripeApiResultProbe` GET PaymentIntent。
缺少 `STRIPE_SECRET_KEY=sk_test_*`、认证失败、未知页面或 Probe 不确定时均 fail closed；`UNKNOWN` 不会重放。
该 hosted flow 只可由显式 smoke 命令触发，尚未接入 M10 的持久 Attempt/Permit runtime：

```powershell
$env:STRIPE_SECRET_KEY = "sk_test_..."
& .venv\Scripts\python.exe scripts\stripe_live_smoke.py --hosted-checkout
```
默认测试、recorded E2E 和 conformance 不联网，也不能伪造 live E2E 通过。

### 模型安全的受约束 Planner

M9 将模型限制为 proposal-only 边界：

- 真实业务值始终留在可信代码中；模型只能看到 Capability、输入槽元数据、步骤角色和脱敏证据 token。
- 身份、租户、Grant、Contract、Policy、Permit、Attempt、审批、浏览器字段、选择器和凭据不得进入模型输入或输出。
- 禁止权限/浏览器字段和语义越权是终止性拒绝，不能通过 repair 修复。
- 只有有限的结构错误允许一次 repair；可信编译器随后重新绑定真实业务输入与权威身份。
- 确定性评估边界覆盖接受、终止拒绝、单次结构修复、值泄漏和保留字段别名等行为。

### 确定性执行闸门

具有外部副作用的 Action 必须经过策略判断、必要审批、基于最新页面观察签发的一次性 `ExecutionPermit`，并先持久化 `ExecutionAttempt`，才能交给 `ActionHandler`。Locator、坐标、JavaScript、CUA 和缓存动作等 fallback 同样受治理，不能成为绕过授权的替代路径。

审批恢复不会复用旧动作或旧观察。审批通过后必须重新观察页面、重新评估策略并签发新的 Permit，才能产生副作用。

### 持久 Journal、UNKNOWN 与有界 Replan

多步骤 Agent Loop 按顺序执行 BusinessPlan，并将计划状态写入哈希链 Journal。已完成前缀不可变；业务状态不匹配时，只允许在预算内重编译未完成后缀。

如果副作用可能已经发生但结果无法确认，Attempt 进入 `UNKNOWN`：

- 禁止使用相同幂等键或相同权限重放动作；
- 禁止在不确定结果上继续 Replan；
- 只能由精确关联 Task、Step、Permit、Attempt、binding 和 probe reference 的独立 Probe 解决；
- Journal 恢复会补齐已由权威证据证明、但因崩溃窗口尚未写入的状态，同时拒绝冲突或无关分支。

### Governed Agent Run API 与操作入口

M10 提供受治理的 Agent Run API 和操作员入口。创建请求经过同一套 Planner、可信编译、Admission、审批、最新观察、Permit、Attempt、原生 Agent 执行与 Probe 路径。公开投影只返回安全的 Pack 元数据、计划摘要、状态、原因码和服务器声明的 `legal_actions`。

操作员只能执行权威详情投影当前允许的 `approve`、`reject`、`cancel` 或 `probe`。列表或前端状态不能自行推断命令权限。

### M11 Provider 组合与操作工作台

M11 将 M10 入口扩展为可恢复的操作工作台：

- Provider 模式只能由服务器配置为 `recorded` 或 `live`；默认为无需凭据的确定性 `recorded`。
- `live` 复用 OpenAI-compatible endpoint、model 和环境凭据配置。配置不完整时启动失败，不会静默回退到 `recorded`。
- Provider 调用在有界的非事件循环线程中执行；模型输出仍必须通过不变的 M9 终止优先校验和可信编译边界。
- 初次创建使用由认证 `tenant_id + request_id` 派生的 PostgreSQL session advisory lock。锁覆盖重新读取、Provider/校验、Admission、审批暂停持久化和权威读回，序列化相同请求 ID 的重复或冲突创建。
- Admission 只持久化安全的 `recorded|live` provenance，不保存 endpoint、model、凭据、prompt、response 或 Provider trace。旧 M10 Admission 默认解释为 `recorded`。
- 租户隔离的历史接口使用稳定游标返回脱敏摘要，不包含 `legal_actions`；跨租户记录与不存在记录不可区分。
- React 工作台用 `?run=<id>` 恢复所选 Run，只轮询当前可见且未终止的详情，并只渲染 root-locked 权威详情返回的 `legal_actions`。

### M12 Agent 评估与决策轨迹

M12 在既有 M9-M11 治理链上增加只读证据面，不引入新的执行权限：

- 每次 Planner 调用生成局部、封闭的安全观察，只记录 `recorded|live` 模式、有限 disposition/code、调用与 repair 次数，以及 Provider 可选返回的非负耗时和 token 计数；不保存 endpoint、model、凭据、prompt、response、业务值或权限句柄。
- 一次结构 repair 成功后的 proposal 会进入与首次接受相同的可信编译路径；权限、浏览器字段和语义越权仍然终止并 fail closed。
- Agent Run 提供租户隔离、root-locked 的非权威 Decision trace，有限阶段为 provider、validation、compilation、admission、approval、execution 和 recovery。轨迹只用于解释与导航，不包含 `legal_actions`，也不参与审批、Permit、Attempt、Replan 或 Probe 判断。
- 下载报告升级为包含同一脱敏轨迹的版本化安全投影；旧 Admission 没有历史 Planner 观察时明确显示 `not_recorded`，不会伪造调用、耗时或 token 数据。
- 确定性与 live-provider 评估通过测试夹具覆盖，不作为应用、演示或发布命令暴露；这些评估不会创建 Task、数据库记录、审批、Permit、Attempt、交互会话或业务副作用。
- 操作工作台仅为所选 Run 加载紧凑 Decision trace，并继续只依据权威详情的 `legal_actions` 渲染命令。

## 当前能力

| 里程碑 | 状态 | 已交付能力 |
|---|---|---|
| M1 契约边界 | Offline | 不可变离线 Contract Catalog；未安装 Pack 默认不可执行 |
| M2 Pack SDK | Offline | 类型化契约、效果、证据、版本规则与确定性 Conformance Kit |
| M3 参考 Pack | Offline | `synthetic.payment` 参考实现和有效/无效 Conformance 夹具 |
| M4 治理 E2E | Offline | loopback Chromium、持久化 Attempt、`UNKNOWN`、禁止重放与独立 recorded Probe |
| M5 开发者体验 | Historical | 合成 CLI、版本锁定和发布证据保留为历史里程碑记录，不再作为当前入口 |
| M6 受约束 Runtime | Interface-only | 安装、身份、租户/RBAC、CapabilityGrant 投影和可信编译接口 |
| M7 原生 Agent 闭环 | Interface-only | ForgeAgent/Chromium、原生 Task/Step、Permit/Attempt 和 Probe 关联接口 |
| M8 顺序 Agent Loop | Interface-only | 多步骤计划、不可变完成前缀、有界后缀 Replan 和持久 Journal 接口 |
| M9 模型安全 Planner | Offline | 值隔离、终止优先拒绝、单次结构修复和确定性评估边界 |
| M10 Agent Run API | Interface-only | Generic Agent Run contracts remain available; Synthetic composition is test-only and is not mounted by formal application startup |
| M11 操作工作台 | Interface-only | recorded/live 服务器组合和安全投影接口；不打开生产 enforce |
| M12 评估与决策轨迹 | Offline tests | 安全 Planner 观察、非权威 Decision trace 和确定性评估夹具 |

## 演进方向

以下内容是演进方向，不代表当前已经实现或具备生产可用性：

- 将合成参考 Runtime 的接口与生命周期进一步抽象，同时保持 Governance Kernel 位于所有浏览器副作用之前。
- 补齐真实 Domain Pack 安装、升级、失效、连接器和权威业务数据源接入流程。
- 完善审计保留、密钥管理、模型地域、容量、可观测性、灾难恢复和受控 rollout 策略。
- 在真实领域采用者提供业务事实、权限来源和独立结果探针之前，继续 fail closed。

长期目标不是让模型获得无限执行权，而是在可验证的领域契约内实现受约束自治。

## 安装与开发验证

支持 Python 3.11、3.12 和 3.13。以下命令安装开发依赖并运行通用平台测试；Synthetic 仅作为测试夹具，不提供 demo、evaluation 或 release CLI。

### Windows PowerShell
```powershell
py -3.11 -m venv .venv
$VenvPython = (Resolve-Path .venv\Scripts\python.exe).Path
& $VenvPython -m pip install -e ".[dev]"
& $VenvPython -m pytest tests\unit\test_governance_benchmark.py tests\unit\test_governance_benchmark_metrics.py tests\unit\test_pack_sdk_static_conformance.py -q
& $VenvPython -m ruff check enterprise\evaluation enterprise\governance\benchmark.py tests\unit\test_governance_benchmark.py tests\unit\test_governance_benchmark_metrics.py tests\unit\test_pack_sdk_static_conformance.py
```
### Linux/WSL
```bash
python3.11 -m venv .venv
VENV_PYTHON="$(pwd)/.venv/bin/python"
"$VENV_PYTHON" -m pip install -e ".[dev]"
"$VENV_PYTHON" -m pytest tests/unit/test_governance_benchmark.py tests/unit/test_governance_benchmark_metrics.py tests/unit/test_pack_sdk_static_conformance.py -q
"$VENV_PYTHON" -m ruff check enterprise/evaluation enterprise/governance/benchmark.py tests/unit/test_governance_benchmark.py tests/unit/test_governance_benchmark_metrics.py tests/unit/test_pack_sdk_static_conformance.py
```

需要 PostgreSQL 或浏览器的场景由对应测试显式配置，不参与默认应用启动。

### 可选 live Provider 配置

默认本地验证使用确定性的 `recorded` 模式，不调用外部模型。只有显式配置完整的服务器环境时才启用 `live`：

```powershell
$env:AGENT_RUN_PROVIDER_MODE = "live"
$env:OPENAI_COMPATIBLE_API_BASE = "https://provider.example/v1"
$env:OPENAI_COMPATIBLE_MODEL_NAME = "model-name"
$env:OPENAI_COMPATIBLE_API_KEY = "<environment-secret>"
```

不要把真实凭据提交到仓库。浏览器/API 请求不能选择 Provider、endpoint、model、凭据或 fallback 行为。

## 合成验证证明什么

合成验证通过 PostgreSQL、真实 Chromium、回环业务控制台和故障注入证明：系统会在浏览器外部效果前持久化 Attempt；无法确认的传输结果进入 `UNKNOWN`；相同动作不会重放；只有独立 Probe 可以确认结果；受治理流程只产生一次预期副作用。

这证明的是参考边界和恢复语义，而不是生产业务正确性、真实连接器可用性或生产容量。

## 边界与限制

- 当前只有 `synthetic.payment` 合成 Domain Pack，没有生产 Pack、真实支付连接器或生产业务数据。
- `live` Provider 是可选服务器配置；测试夹具与默认本地验证保持确定性的 `recorded` 模式，不依赖外部网络。
- 不包含生产凭据管理、租户安装、迁移、生产 rollout、全局 enforce 或可运营金融系统能力。
- 浏览器传输成功不会被直接视为业务结果确认；`UNKNOWN` 必须通过独立 Probe 解决。
- 本仓库是开发参考实现和证据验证工具，尚未达到生产就绪状态。

历史 M5 复现步骤和当时的兼容性声明保留在 [M5 开发者指南](docs/phase-2/m5-developer-release-guide.md) 与 [产品章程](docs/phase-2/final-product-charter.md) 中，不代表当前支持的命令。上游声明请参阅 [NOTICE](NOTICE.md)。公开仓库地址为 [meizhouyu666/agentpact](https://github.com/meizhouyu666/agentpact)。

## 许可证与声明

本仓库采用 [MIT License](LICENSE)，并基于 [Skyvern](https://github.com/Skyvern-AI/skyvern) 构建。上游版权、许可和归属信息详见 [NOTICE](NOTICE.md)。
