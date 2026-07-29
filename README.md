# AgentPact

> Governed Browser-Agent Harness, Domain Pack SDK & Conformance Kit

AgentPact 是一个面向高风险浏览器任务的 Agentic RPA 执行与恢复框架。它关注的
不只是 Agent 能否完成任务，还包括 Agent 是否有权执行、结果不确定时能否安全
停止，以及如何通过独立证据确认真实业务状态。

项目基于 Skyvern 的浏览器 Agent Loop 构建，在 LLM 决策与 Playwright 副作用之间
加入类型化领域契约、确定性授权、一次性执行许可、持久化尝试状态和分级恢复机制。
当前版本仅使用合成数据，包括合成身份、页面、业务事实与故障注入，不面向生产环境。

## 为什么需要 AgentPact

传统浏览器自动化通常把“点击成功”视为任务成功，但 Agent 面对真实业务操作时，
还需要回答更困难的问题：

- 自然语言目标是否对应当前用户已获授权的业务能力？
- LLM 选择的页面动作是否仍然符合原始业务意图和当前页面证据？
- 提交后连接超时，操作究竟失败了，还是已经产生外部副作用？
- 重新执行是在恢复任务，还是在制造重复付款、重复订单或重复审批？

AgentPact 将这些问题放入浏览器执行主链的边界设计中。浏览器传输成功不等于业务
成功；执行结果不确定时，Agent 必须停止重放，并通过独立 Result Probe 重新观察
目标系统。

> Agent 不应该在“不知道有没有成功”时，再点一次提交。

## 工作方式

```mermaid
flowchart LR
    U[自然语言任务] --> I[身份与租户上下文]
    I --> C[Capability 与 Domain Pack]
    C --> P[受约束 Planner / BusinessPlan]
    P --> W[ExecutionWorkOrder]
    W --> S[Skyvern Agent Loop]
    S --> G[Governance Kernel]
    G -->|Permit| A[ActionHandler / Playwright]
    A --> R[BusinessResultProbe]
    R --> D[确认、恢复或人工介入]
```

架构将业务规划、任务编排和页面动作分开：

- **Domain Pack** 定义业务能力、输入、状态转换、风险和结果证据，不接触 DOM 或
  Playwright。
- **Planner** 只能在当前身份和租户获得的 `CapabilityGrant` 范围内生成
  `BusinessPlan`，不能自由发明业务操作。
- **ExecutionWorkOrder** 将业务计划转换为带有允许操作、禁止操作、成功条件和恢复
  上限的浏览器任务。
- **Skyvern Agent Loop** 继续负责 DOM/截图感知、页面内动作决策和浏览器级恢复，
  `ActionHandler` 是唯一浏览器执行器。
- **Governance Kernel** 在副作用发生前完成策略、审批、Permit 和 Attempt 裁决；
  **Result Probe** 独立确认最终业务结果。

## 核心能力

### 受约束的 Agent 规划

`Capability -> BusinessPlan -> ExecutionWorkOrder -> Action` 分层契约将 LLM 业务
规划与底层页面动作解耦。身份、租户、数据范围和 RBAC 来自可信上下文，确定性
校验器拒绝越权 Capability、过期 Grant 和扩大业务范围的 Replan。

### Domain Pack SDK 与 Conformance Kit

Domain Pack SDK 使用版本化、类型化契约描述 Agent 可调用能力、业务事实、效果
分类、状态机、证据要求和 Result Probe。离线 Contract Catalog 与未来的活动运行时
注册表严格隔离；Conformance Kit 接受合法参考 Pack，并确定性拒绝缺少来源、所有者、
证据或安全语义的 Pack。

### 确定性执行闸门

具有外部副作用的 Action 必须经过策略判断、审批、一次性 `ExecutionPermit` 和
持久化 `ExecutionAttempt`，才能交给 `ActionHandler`。locator、坐标、JavaScript、
CUA 和缓存动作等 fallback 也属于治理范围，不能成为绕过授权的替代路径。

### L0-L4 分级恢复

恢复机制区分局部动作恢复、页面重新感知、Step 重试、业务 Replan，以及治理或人工
介入。可能已经产生副作用但结果无法确认的 Attempt 会进入 `UNKNOWN`，相同幂等键
的重放将被拒绝，最终状态只能由独立探针确认。

### 可审计证据与故障验证

版本化证据覆盖 Contract、Grant、Plan、Work Order、Permit、Attempt、Observation
和 RecoveryDecision。合成基准通过 Chromium、PostgreSQL、回环业务控制台和故障
注入验证 post-effect timeout、`UNKNOWN`、禁止重放、独立确认和单次提交。

## 当前能力

| 里程碑 | 状态 | 已交付能力 |
|---|---|---|
| M1 契约边界 | 已完成 | 不可变离线 Contract Catalog 与未来活动注册表隔离，未安装 Pack 默认不可执行 |
| M2 Pack SDK | 已完成 | 可复用契约、效果、证据和版本规则，以及确定性静态 Conformance Kit |
| M3 参考 Pack | 已完成 | `synthetic.payment` 参考实现、有效/无效 Pack 夹具和隔离测试 |
| M4 受治理 E2E | 已完成 | 真实 Chromium 副作用、持久化 `EXECUTING/UNKNOWN`、禁止重放、独立探针与完整清理 |
| M5 开发者体验 | 已完成 | 跨平台 CLI、版本锁定、机器可读报告、Ubuntu CI 与 Windows smoke 路径 |
| M6 受约束 Agent Runtime | 已完成 | 从 Domain Pack 安装、身份/租户/RBAC 与 CapabilityGrant 投影，到 BusinessPlan 和 ExecutionWorkOrder 的确定性编译 |
| M7 原生 Agent 闭环 | 已完成 | 真实 ForgeAgent/Chromium 执行、原生 Task/Step 绑定、Permit/Attempt 闸门、UNKNOWN 探针恢复与单次副作用证明 |
| M8 治理式多步骤 Loop | 已完成 | 顺序多步骤 BusinessPlan、不可变已完成前缀、受限 L3 后缀 Replan、哈希链 Journal 与崩溃恢复 |

M6-M8 已将这条链路推进为可运行的合成 Agent Runtime：Planner 负责业务计划，
Skyvern Agent Loop 负责页面感知，Governance Kernel 负责确定性执行闸门和恢复。
当前仍是合成数据、回环控制台和本地 PostgreSQL/Chromium 证明，不是生产部署声明。

## 未来蓝图

以下内容是演进方向，不代表当前已经实现或具备生产可用性。

### 1. 从合成 Runtime 走向可复用 Runtime

- 将当前 synthetic-only 的多步骤协调器抽象为可复用的 Runtime 接口，并保持治理闸门
  位于所有浏览器副作用之前。
- 引入租户级 `DomainPackInstallation` 与只加载已安装 Pack 的 Active Registry，
  再把 Capability 投影为 Planner 可见的结构化工具。
- 在第二个小型 Domain Pack 上复用 `BusinessPlan -> ExecutionWorkOrder ->
  Skyvern Task/Step` 与 Journal/Probe 契约。

### 2. 验证领域扩展能力

- 增加第二个小型合成 Domain Pack，证明 SDK 没有与支付场景写死。
- 完善 Adapter/Probe 生命周期、版本兼容、升级和失效语义。
- 扩展 Conformance Kit，覆盖安装、运行时解析、Planner 工具投影和跨版本契约测试。

### 3. 走向可控试运行

- 先以 tenant/workflow/Domain Pack 为粒度启用 audit 和 rehearsal，再讨论 scoped enforce。
- 补齐 DOM、截图、Prompt、模型地域、保留期和访问审计的数据策略。
- 由真实领域采用者提供业务事实、权限、权威数据源和结果探针；没有这些材料时继续
  fail closed。

长期目标不是让 LLM 获得无限执行权，而是在可验证的领域契约内实现受约束自治。

## 运行合成验证

支持 Python 3.11、3.12 和 3.13。PostgreSQL 14+ 需要在 `PATH` 中提供
`initdb`、`pg_ctl`、`createdb` 和 `pg_isready`。以下命令均可直接在仓库根目录
执行，并始终使用虚拟环境中的 Python。为保持兼容，命令入口仍为
`scripts/finrpa_release.py`。

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
$VenvPython = (Resolve-Path .venv\Scripts\python.exe).Path
& $VenvPython -m pip install -e . -r requirements-m5-demo.lock
& $VenvPython -m playwright install chromium
& $VenvPython scripts\finrpa_release.py doctor
& $VenvPython scripts\finrpa_release.py conformance
& $VenvPython scripts\finrpa_release.py demo
& $VenvPython scripts\finrpa_release.py report
```

### Linux/WSL

```bash
python3.11 -m venv .venv
VENV_PYTHON="$(pwd)/.venv/bin/python"
"$VENV_PYTHON" -m pip install -e . -r requirements-m5-demo.lock
"$VENV_PYTHON" -m playwright install chromium
"$VENV_PYTHON" scripts/finrpa_release.py doctor
"$VENV_PYTHON" scripts/finrpa_release.py conformance
"$VENV_PYTHON" scripts/finrpa_release.py demo
"$VENV_PYTHON" scripts/finrpa_release.py report
```

macOS 仅提供尽力支持，不作为发布门禁。常规自动发现不可用时，可将
`FINRPA_POSTGRES_BIN` 指向 PostgreSQL 二进制目录，或将
`FINRPA_CHROMIUM_EXECUTABLE` 指向已安装的 Chromium 可执行文件。这些配置项只
接受可执行路径，不用于传递凭据。

命令成功时返回 `0`；缺少前置条件、前置条件不安全或证据无效时返回 `2`；
conformance/demo 检查失败时返回 `3`。成功的 `conformance` 和 `demo` 会将符合
`finrpa.release-report/v1` 的 JSON 与 Markdown 证据写入已忽略的
`artifacts/m5/` 目录；`report` 会先验证证据摘要，再渲染报告。

## 合成验证证明什么

M4 验证会在浏览器产生外部效果前持久化记录 `EXECUTING`，将无法确认的传输结果
记录为 `UNKNOWN`，拒绝使用同一幂等键重放，最后仅通过独立调用的探针确认业务
结果。整个过程只会向一次性回环控制台提交一次合成操作。

验证成功前，清理流程会关闭 Chromium 和 Uvicorn、停止 PostgreSQL、关闭回环
端口，并删除经过校验的临时目录。浏览器传输成功绝不会被直接视为业务结果确认。

## 边界与限制

- 不包含真实支付数据、凭据、生产 API 调用或生产 Domain Pack。
- 不包含部署、软件包发布、迁移、租户安装或生产运行路径。
- 仅在合成 payment Domain Pack、回环控制台、本地 PostgreSQL/Chromium E2E 中接入
  Planner/ForgeAgent；生产 Active Registry、真实站点和真实凭据仍未接入。
- 全局 `GOVERNANCE_MODE=enforce` 仍会被配置校验拒绝。
- 本仓库是开发参考实现和证据验证工具，不是可运营的金融系统。

更详细的复现步骤、限制、许可证与上游声明，请参阅
[M5 开发者指南](docs/phase-2/m5-developer-release-guide.md)、
[产品章程](docs/phase-2/final-product-charter.md)和 [NOTICE](NOTICE.md)。
公开仓库地址为 [meizhouyu666/agentpact](https://github.com/meizhouyu666/agentpact)。

## 许可证与声明

本仓库采用 [MIT License](LICENSE)，并基于
[Skyvern](https://github.com/Skyvern-AI/skyvern) 构建。上游版权与许可证信息详见
[NOTICE](NOTICE.md)。
