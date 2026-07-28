# AgentPact

> Governed Browser-Agent Harness, Domain Pack SDK & Conformance Kit

AgentPact 是一个基于 Skyvern 构建的受治理浏览器智能体参考实现，仅使用合成数据，
不面向生产环境。项目展示了类型化 Domain Pack 契约、一次性执行许可、持久化尝试
状态、`UNKNOWN`/禁止重放恢复机制，以及不依赖真实支付系统的独立结果确认流程。

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

## 验证内容

Skyvern 的 `ActionHandler` 始终是唯一的浏览器执行器。M4 验证会在浏览器产生
外部效果前持久化记录 `EXECUTING`；将无法确认的传输结果记录为 `UNKNOWN`；拒绝
使用同一幂等键进行重放；最后仅通过独立调用的探针确认业务结果。整个过程只会向
一次性回环控制台提交一次合成操作。

验证成功前，清理流程会关闭 Chromium 和 Uvicorn、停止 PostgreSQL、关闭回环
端口，并删除经过校验的临时目录。浏览器传输成功绝不会被直接视为业务结果确认。

## 边界与限制

- 不包含真实支付数据、凭据、生产 API 调用或生产 Domain Pack。
- 不包含部署、软件包发布、迁移、租户安装或生产运行路径。
- 未接入 active registry，也未启用 Planner/ForgeAgent 运行时连线。
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
