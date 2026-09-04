# `stripe.payment` Domain Pack — 真包骨架与采纳指南

> 状态：**test-mode candidate**。recorded 基线保持离线；显式 live flow 已接入真实
> Stripe test API hosted Checkout 和独立 PaymentIntent Probe。该 flow 不是生产安装，
> 不启用 `GOVERNANCE_MODE=enforce`，也不接受 `sk_live_*`。

## 1. 这个包解决什么问题

现有 `synthetic.payment` 证明了治理链（Permit/Attempt/UNKNOWN/Probe/Journal）的
正确性，但它的"权威业务状态"和"浏览器"来自同一个自建的回环控制台——Probe 查的是
自己造的假系统。批评者可以说：**探针和浏览器是同一个作者写的，凭什么信它？**

`stripe.payment` 把同一条治理链对接到**真实外部系统**（Stripe 测试模式）：

- 浏览器副作用 → 在真实 Stripe 测试收银页/测试商户后台操作（`ActionHandler` 唯一入口）
- 权威读 + 独立 Probe → `GET https://api.stripe.com/v1/payment_intents/{id}`（test key）
- 浏览器与 API 是**两家独立系统**——这是对"UNKNOWN 只能由独立 Probe 解决"论点的
  质的增强：从"我们这么设计"变成"在真实外部系统上证明了它"

配套收益：Stripe 的 **Idempotency-Key** 语义与框架"禁止用相同幂等键重放"一一对应；
`payment_intents` 的 `succeeded/processing/requires_*/canceled` 状态机天然映射
`CONFIRMED/UNKNOWN/NOT_CONFIRMED`。测试密钥（`sk_test_*`）无生产风险。

## 2. 文件清单与完成状态

| 文件 | 状态 | 说明 |
|---|---|---|
| `constants.py` | ✅ 完成 | Pack 身份、证据 ref、部署占位身份 |
| `models.py` | ✅ 完成 | Stripe 事实模型（整数最小货币单位）、受治理记录、错误分类 |
| `sdk_manifest.py` | ✅ 完成 | 离线不可变 SDK 契约（`EXTERNAL_CANDIDATE`），通过静态 Conformance |
| `definition.py` | ✅ 完成 | 活动 `DomainPackManifest`（`kind=PRODUCTION`，`production_eligible=False`） |
| `policy.py` | ✅ 完成 | 确定性策略：恒审批、双控、金额阈值、职责分离（规则 id `stripe.payment.*`） |
| `result_probe.py` | ✅ 完成（逻辑） | `StripeApiResultProbe`（live）+ `RecordedStripeProbe`（recorded）+ 状态映射 |
| `live_browser.py` | ✅ 完成（显式 live smoke） | Stripe test API Checkout Session + hosted `checkout.stripe.com` + 独立 PaymentIntent Probe；脱敏证据 |
| `m6_runtime.py` | ✅ 完成（核心） | M6 受治理编译/绑定/追踪镜像 + `probe_submission_outcome`/`require_confirmed_outcome` 接线 |
| `m10_runtime.py` | ✅ 完成（适配器） | `StripePaymentRuntimeAdapter`（recorded 全流程；live 仅接受显式注入的 hosted Checkout adapter） |
| `store.py` | ✅ 完成 | 模拟 Stripe 测试后端（PaymentIntent 状态机 + 故障注入）+ recorded 权威探针 |
| `harness.py` | ✅ 完成 | 受治理执行 harness：审批→Permit→Attempt→Probe 收口、禁重放、授权失效 |
| `app.py` | ✅ 完成 | "Stripe 测试收银台"控制台：checkout 通道 + `/v1/payment_intents` 权威读通道 |
| `accounts.py` | ✅ 完成 | 沙箱测试身份（operator/approver/compliance/viewer） |
| `PACK.md` | ✅ 本文档 | 采纳指南与 TODO |
| `tests/unit/test_stripe_payment_pack_conformance.py` | ✅ 完成 | 静态 Conformance 门禁（确定性、无网络） |
| `tests/unit/test_stripe_payment_probe.py` | ✅ 完成 | Probe 状态映射/recorded/脱敏测试 |
| `tests/unit/test_stripe_payment_m6_runtime.py` | ✅ 完成 | M6 编译/绑定/追踪 + Probe 接线测试（16 个，确定性） |
| `tests/unit/test_stripe_payment_m10_runtime.py` | ✅ 完成 | M10 适配器/registry 协议测试（8 个，确定性） |
| `tests/unit/test_stripe_payment_harness.py` | ✅ 完成 | 全流程/UNKNOWN 恢复/禁重放/职责分离（13 个） |
| `tests/unit/test_stripe_payment_app.py` | ✅ 完成 | 控制台双通道 API 冒烟（4 个） |
| `tests/e2e/test_stripe_payment_governed_browser.py` | ✅ 完成（recorded） | 自建 loopback checkout 的 Chromium 治理证明；不是 Stripe hosted E2E |
| `scripts/stripe_live_smoke.py --hosted-checkout` | ✅ 显式手工命令 | 有 test key 时创建真实 hosted Checkout、使用 4242、再 GET PaymentIntent；默认不执行 |

**设计说明**：`store.py` 和 `app.py` 是 recorded 模式的**自建测试 checkout**，不是 Stripe。
真实 test-mode 语义只在显式 `live_browser.py` flow 中成立：Stripe API 创建 hosted
Checkout Session，浏览器访问 `checkout.stripe.com`，完成后独立 `GET /v1/payment_intents/{id}`。
三者不能互相冒充，默认单测和 recorded conformance 永不联网。

## 3. 分阶段实现清单

### P0 — 静态契约收口（当前已完成，可验证）

- [x] `sdk_manifest.py` 通过 `evaluate_static_pack_conformance`（PASS）
- [x] `PACK_CONFORMANCE_MANIFEST_DIGEST` 已回填为真实摘要
- [x] 单元测试覆盖：只读权限拒绝、证据新鲜度、未知生命周期、引用缺失、
      外部写缺 Probe、离线无运行时导入
- 验证命令：

```powershell
& .venv\Scripts\python.exe -m pytest tests/unit/test_stripe_payment_pack_conformance.py -q
& .venv\Scripts\python.exe -m ruff check enterprise/domains/stripe_payment
```

### P1 — Probe 收口（核心已完成，字段已对照官方文档核对）

- [x] 状态映射 `classify_payment_intent`：`succeeded→CONFIRMED`、
      `processing/requires_*→UNKNOWN`、`canceled→NOT_CONFIRMED`、未知状态→UNKNOWN
- [x] 网络故障/5xx/超时→UNKNOWN；404→NOT_CONFIRMED；401/403→抛错 fail closed
- [x] **对照 Stripe API 文档核对字段**（docs.stripe.com/api/payment_intents）：
      `status` 全量 7 值已收录（含 `requires_capture`，映射 UNKNOWN）；
      `amount`（整数最小货币单位）、`currency`（小写 ISO 4217）、
      `failure_code`/`failure_message`（可空）与读模型一致
- [x] 幂等键派生：受信任编译器从 `payment_intent_id` 派生 `stripe:{payment_intent_id}`
      （已在 `m6_runtime.py` 落地，`harness.py` 用 `stripe:{challenge_id}` 做尝试级键），
      Probe 只接收摘要用于关联，不落明文
- [x] live 冒烟脚本 `scripts/stripe_live_smoke.py`：显式手工模式，`sk_test_*` 强制校验
      （live key 直接拒绝），`--create` 创建/取消一个测试 PaymentIntent 走通
      CONFIRMED/NOT_CONFIRMED/UNKNOWN 三路径；无 key 或非 test key 退出码 2
- [x] **真实 `sk_test_*` 冒烟已完成**（2026-08-27，真实 Stripe API）：
      确认支付 `succeeded`→CONFIRMED；未确认 `requires_payment_method`→UNKNOWN；
      取消 `canceled`→NOT_CONFIRMED——三路径全部符合设计。本机直连偶发
      `SSL: UNEXPECTED_EOF`，经 `http://127.0.0.1:7897` 代理稳定通过

### P2 — 浏览器路径（recorded 与显式 live hosted flow）

已选择并完成**自建测试收银台**方案（`app.py`，双通道：checkout 提交 + `/v1/payment_intents`
权威读），`store.py`（故障注入）/`harness.py`（审批→Permit→Attempt→Probe 收口）为
确定性闭环，13 个 harness + 4 个 API 测试通过。剩余事项：

- [x] recorded 目标页面：自建 FastAPI 测试 checkout 页（`data-governance-*` 字段齐全，
      仅供 loopback/recorded 语义定位）
- [x] live 目标页面：Stripe API 创建的 hosted Checkout Session，浏览器必须访问
      `https://checkout.stripe.com/c/...`，不能把自建页面称为真实 Stripe E2E
- [x] `ExecutionWorkOrder` 的 `navigation_goal` / `allowed_operations` /
      `prohibited_operations` 已在 `m6_runtime.py` 与 `harness.py` 定义
- [x] 确定性闭环：正常提交→CONFIRMED；commit_then_timeout→Probe 确认；
      commit_then_inconclusive→UNKNOWN→Probe 恢复；processing/canceled→fail closed
- [x] recorded Chromium e2e（`tests/e2e/test_stripe_payment_governed_browser.py`）：
      loopback console + PostgreSQL + governed harness 的 UNKNOWN→Probe 闭环；不是
      Stripe hosted E2E，也不产生真实 Stripe 证据
- [x] live hosted flow（`live_browser.py`）：API 创建 Session，完成 Stripe 4242
      测试卡，再使用独立 `StripeApiResultProbe` 读取 PaymentIntent；无凭据时不伪造通过

### P3 — 运行时装配（核心已完成，余项见下）

`m6_runtime.py` 已镜像完成（`STRIPE_RUNTIME_CONTRACT` + 固定 digest 证明、
安装构建、`compile_stripe_request`、execution binding / permit binding、
`append_execution_trace`、`probe_submission_outcome` / `require_confirmed_outcome`
接线），16 个确定性单测通过。剩余事项：

- [x] `m6_runtime.py`：`STRIPE_RUNTIME_CONTRACT`、安装构建、`compile_stripe_request`
      （Planner→TaskContract→BusinessPlan→ExecutionWorkOrder）、execution binding /
      permit binding、`append_execution_trace`
- [x] 关键差异点：`probe_submission_outcome` 用 `RecordedStripeProbe` /
      `StripeApiResultProbe` 做独立判定；`require_confirmed_outcome` 对
      `UNKNOWN`/`not_confirmed` fail closed（禁重放）
- [ ] 替换身份常量：`TENANT_ID`/部门/业务线**必须由采纳租户提供**，当前是占位值
- [ ] 浏览器路径接入（P2 完成后）：把 `observed_business_inputs` 从 ActionHandler
      取回，执行后调用 `probe_submission_outcome`，再进 `append_execution_trace`
- [x] **M10 适配器**（`m10_runtime.py`）：`StripePaymentRuntimeAdapter` 完整实现
      `PackRuntimeAdapter` 协议（binding / model_safe_projection / prepare_run /
      restore_run / admit_run / advance_run / probe_run），复用 M6 编译 + harness
      确定性执行 + admission 原语；recorded 模式全流程可用，live 模式只有显式
      注入 `StripeHostedCheckoutFlow` 才能执行，否则 `StripeM10NotWired` fail closed
- [x] **M10 平台侧多包化**：`AgentRunService` 的 approve/advance/reject
      状态追踪使用包无关的 journal/checkpoint 与 runtime binding；平台 registry
      可显式注册多个 Pack，Stripe 通过 `compose_stripe_agent_run_service` 接入。
      formal app 仍保持空 registry，Stripe 不会被默认安装或启用 enforce
- [ ] 决定 `GovernanceMode`：P2/P3 阶段用 `AUDIT` 演练，不得启用 `enforce`
      （README：enforce 配置被拒，未来需单独批准）

### P4 — 演示与评估

- [ ] `RecordedStripeProbe` 夹具：succeeded / processing→UNKNOWN / canceled /
      404 / 超时 五条确定性路径（无网络、无凭据）
- [x] 录制 demo：现有测试和独立脚本保留 recorded 证据；live smoke 不默认执行
- [ ] 评估：把 `stripe.payment` 加入 `agentpact_eval.py` 的 recorded 案例集
- [x] 文档：README、Phase 2 status 与本文档明确区分 loopback、Stripe API Probe、
      hosted Checkout E2E；M1-M12 使用 Active runtime / Interface-only / Offline / Dormant

## 4. 凭据与安全规则（不可违反）

- 只使用 `sk_test_*`；**绝不使用 live key**，绝不把任何密钥提交到仓库
- `STRIPE_SECRET_KEY` 从环境变量读取；`StripeApiResultProbe` 缺配置时
  **构造即失败**，不静默回退 recorded 模式（对齐 README 的 live 配置语义）
- Probe 证据只记录 `stripe_status`/`failure_code`/idempotency 摘要等脱敏元数据，
  不落 prompt、response、凭据
- CI 与默认验证保持确定性 recorded 模式（`RecordedStripeProbe`），不依赖外网

## 5. 诚实边界

- 本包仍是 **test-mode candidate**：recorded 运行时和显式 hosted flow 已接入，
  但没有生产安装、生产凭据管理或生产 enforce 资格
- Stripe 测试模式证明的是"真实外部系统上的治理边界"，不是生产支付正确性
- `kind=PRODUCTION` 但 `production_eligible=False`：真实领域、未获准生产，
  正是 README「在真实领域采用者提供业务事实、权限来源和独立结果探针之前，
  继续 fail closed」的落地形态

## 6. 与 `synthetic.payment` 的对应关系

| 维度 | synthetic.payment | stripe.payment |
|---|---|---|
| Pack id | `synthetic.payment` | `stripe.payment` |
| SDK kind | `SYNTHETIC_REFERENCE` | `EXTERNAL_CANDIDATE` |
| 活动 kind | `SYNTHETIC`（强制 `synthetic.` 前缀） | `PRODUCTION`（`production_eligible=False`） |
| 权威源 | `synthetic.payment.store/v1`（回环） | `stripe.api/v1`（真实 API） |
| Probe | 查询自建 store | `GET /v1/payment_intents/{id}` |
| 幂等键 | `synthetic:{payment_id}` | `stripe:{payment_intent_id}`（待 P1 落实） |
| 生命周期 | `draft→submitted` | 同左（业务终态由 Probe 判定） |
| 策略规则 | `synthetic.payment.*` | `stripe.payment.*`（同结构） |
| 控制台 | `app.py` 合成控制台 | 不需要（P2 自建测试 checkout 页可选） |

## 7. 当前运行时状态（权威说明）

| 路径 | 状态 | 允许的结论 |
|---|---|---|
| `app.py` + `store.py` | Offline / recorded | 自建测试 checkout 的治理和 UNKNOWN 语义；不是真实 Stripe 证据 |
| `result_probe.py` `RecordedStripeProbe` | Offline / recorded | 无网络的确定性单测与 conformance |
| `live_browser.py` | Active runtime / explicit smoke | 真实 Stripe test API + hosted Checkout + 独立 PaymentIntent GET；需要人工提供 `sk_test_*` |
| `m10_runtime.py` recorded adapter | Active runtime / recorded | 默认无凭据、无网络的 M10 适配器 |
| `m10_runtime.py` live adapter | Active runtime / explicit composition | 可执行，但必须显式注入 `StripeHostedCheckoutFlow` 与持久化 Attempt/Permit session；缺任一项即 fail closed，不回退 recorded |

Live 路径只从 `STRIPE_SECRET_KEY` 环境变量读取 `sk_test_*`。缺少配置、live key、
认证失败、未知 hosted 页面、网络/超时、`processing` 或 `requires_*` 都不能报告成功；
UNKNOWN 只允许重新 GET Probe，绝不自动重放 Checkout。证据只保存摘要、状态和不含凭据
的标识。没有真实凭据时，不能把任何离线或 loopback E2E 称为 live Stripe E2E。
## Review boundary

`live_browser.py` is an explicit test-mode hosted Checkout adapter. It uses the
real Stripe API, visits `https://checkout.stripe.com/c/...`, and uses an
independent PaymentIntent GET probe. It is not the recorded `app.py`/`store.py`
loopback console.

`StripePaymentRuntimeAdapter` can execute the live M10 path only when callers
explicitly inject a `StripeHostedCheckoutFlow` and a durable Attempt/Permit
session factory. The adapter then crosses the persisted browser boundary and
leaves uncertain outcomes for the independent Stripe probe; missing wiring,
credentials, approval/capability expiry, unsafe browser state, and inconclusive
reads fail closed. The live path remains a test-mode candidate and is not
production-eligible.

## Live boundary (authoritative)

The hosted Stripe flow is an explicit test-mode candidate with a governed M10
execution path. Live `advance_run` and `probe_run` are executable only with an
injected hosted flow and durable `ExecutionAttempt`/`ExecutionPermit` lifecycle
around the browser side effect and its independent result probe; otherwise they
fail closed.

## Current review decision

The hosted Checkout implementation is an explicit test-mode candidate, and the
injected live M10 composition is executable through the persisted governance
boundary. Any invocation without the explicit hosted flow or durable
Attempt/Permit persistence remains fail closed. Recorded/offline execution is
still the default conformance path, and this pack remains production-ineligible
until separately approved with production credentials and deployment controls.
