# X 多账户自动化系统

这是一个 Python 3.12+ 单仓库，按应用、公共包和跨模块文档组织。

## 目录结构

```text
auto/
├── apps/
│   └── browser-custom/            # CloakBrowser 浏览器账户管理应用
│       ├── src/browser_custom/    # Python 包和 Web 静态资源
│       ├── tests/                 # browser-custom 测试
│       ├── config/                # 示例配置及本地运行配置
│       ├── pyproject.toml
│       └── README.md
│
├── packages/
│   └── x-actions-playwright/      # X/Twitter Playwright 原子动作包
│       ├── src/x_actions_playwright/
│       ├── tests/
│       ├── pyproject.toml
│       └── README.md
│
├── docs/                          # 跨模块架构和设计文档
└── README.md                      # 仓库总览
```

未来任务系统建议增加为：

```text
apps/x-ops/
```

Task Program、Task SDK、Task Runner、管理后台和任务持久化放在 `x-ops` 内；
`browser-custom` 和 `x-actions-playwright` 继续保持独立边界。

## 模块

### browser-custom

只负责 CloakBrowser、persistent profile、代理、指纹、插件和浏览器生命周期。

- [模块说明](apps/browser-custom/README.md)
- [模块配置](apps/browser-custom/pyproject.toml)

### x-actions-playwright

只负责使用 Playwright 执行 X/Twitter 原子动作，不启动浏览器、不调度任务。

- [模块说明](packages/x-actions-playwright/README.md)
- [模块配置](packages/x-actions-playwright/pyproject.toml)

## 设计文档

- [脚本式任务系统架构](docs/script-task-system-architecture.md)
- [Task Runner 详细处理流程](docs/task-runner-processing-design.md)
- [管理后台功能与组织设计](docs/admin-console-design.md)
- [可视化工作流引擎设计（冻结）](docs/visual-workflow-engine-design.md)

## 安装

在仓库根目录执行：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools
python -m pip install -e "apps/browser-custom[dev]"
python -m pip install -e "packages/x-actions-playwright[dev]"
```

## 运行 browser-custom

```bash
cd apps/browser-custom
cp config/accounts.example.json config/accounts.json  # 首次空配置时可选
python -m browser_custom
```

打开 <http://127.0.0.1:8787/>。

如果已经存在 `apps/browser-custom/config/accounts.json`，不要再次复制示例配置覆盖它。

## 测试

```bash
cd apps/browser-custom
pytest
```

```bash
cd packages/x-actions-playwright
pytest
```
