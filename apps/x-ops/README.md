# x-ops

`x-ops` 是多账户 X 任务系统的运行与管理应用。每个 `TaskRun` 只执行一个账户；
任务程序包含完整业务逻辑，Task SDK 只提供公共服务，Task Runner 只负责运行环境。

项目包含 FastAPI 管理 API、中文 Web 管理台和轻量调度器，但不包含可视化工作流
运行时、StepRun、Checkpoint 或自动整任务重试。

## 模块边界

- `task_programs`：完整的浏览、匹配和互动业务逻辑。
- `task_sdk`：只向 `TaskContext` 暴露 `account`、`actions`、`ai`、`logger` 和
  `cancellation` 五项能力。
- `runner`：原子领取、同账户互斥、浏览器并发槽位、生命周期、协作式取消、
  结果映射和安全清理。
- `integrations`：对 `browser-custom` 和 `x-actions-playwright` 的薄适配层。
- `storage.py`：只持久化 `tasks`、`task_runs` 和 `task_logs` 三张 SQLite 表。
- `api`、`web`、`scheduler.py`：管理 API、管理界面和统一任务触发调度。

`JsonAccountStore` 只保存非敏感的 X 业务账户元数据；代理密码、指纹和 Profile
配置继续由 browser-custom 保存。

## 安装与运行

在仓库根目录的 Python 3.12+ 虚拟环境中，按顺序安装三个本地包：

```bash
python -m pip install -e "apps/browser-custom[dev]"
python -m pip install -e "packages/x-actions-playwright[dev]"
python -m pip install -e "apps/x-ops[dev]"
python -m x_ops
```

管理后台默认监听 <http://127.0.0.1:8790/>。可通过 `X_OPS_HOST`、`X_OPS_PORT`
和 `X_OPS_LOG_LEVEL` 修改监听参数。

默认数据目录固定为 `apps/x-ops/data/`，不会随启动时的当前目录变化。可用
`X_OPS_DATA_DIR` 覆盖。该目录包含任务 SQLite、业务账户映射、运行设置和 AI
非敏感配置，不应提交到 Git。

## 浏览器集成

生产环境的 `BrowserCustomGateway`、管理后台浏览器控制和 browser-custom
`SessionRegistry` 默认运行在同一个 Python 进程，因为 Playwright `Page` 是进程内
对象。这样也能避免两个进程同时占用同一 persistent profile。

Task Runner 通过 `SessionRegistry.acquire_page()` 创建 TaskRun 独占的任务 Page。
`BrowserLease.release(close_browser=...)` 会先关闭本次租约拥有的 Page；当策略为
`close` 时，再委托 `SessionRegistry.close()` 关闭整个账户浏览器。清理警告会保存，
但不会覆盖已经确定的任务结果。

browser-custom 原有的 8787 独立页面仍可用于单独调试浏览器，但运行 x-ops 时不应再
启动另一个 browser-custom 进程去管理同一批 Profile。统一管理后台已将同一个
browser-custom 应用挂载到 `/browser-custom/`，可在同一进程内配置代理、指纹、
Profile、插件和浏览器账户。

## 测试

```bash
cd apps/x-ops
python3.12 -m pytest
ruff check .
mypy src
```
