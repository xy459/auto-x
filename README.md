# browser-custom

独立的 **CloakBrowser + Playwright persistent context** 浏览器账户管理服务，从
`auto-chat` 的浏览器层迁移而来，不包含聊天、站点适配、任务编排、通知或扩展架构。

## 核心约束

- 只支持 CloakBrowser + Playwright。
- 每个账户必须使用独立的绝对路径 `userDataDir`。
- 每账户独立进程树、代理、指纹和登录状态。
- 指纹 seed 由不可变账户 ID 稳定派生，同账户跨启动一致、不同账户不同。
- 默认不覆盖平台、系统版本或 UA 品牌版本，由 CloakBrowser 基于 seed 自动生成一致身份。
- 浏览器二进制支持 Stable / Preview 通道、实际解析版本展示，以及可选的精确版本锁定。
- 支持单个或勾选批量打开、关闭，以及单个重启、状态和进程内存统计。
- 支持在状态列即时刷新；用户手动退出浏览器后，会结合 Playwright context 和实际进程重新判定状态。
- persistent profile 默认允许扩展运行；可在 headed 浏览器的 `chrome://extensions/` 中手动加载扩展并随账户目录持久保存。
- 支持通过全局 `extensionPaths` 自动向所有账户加载一组已解压扩展，并通过批量重启应用插件更新。
- 代理、出口 IP、时区、语言和 WebRTC 使用 CloakBrowser 官方 `geoip=True` 统一解析；启动时不信任历史出口 IP。
- 支持自动匹配、手动覆盖和关闭地区匹配三种模式，并可在严格代理模式下解析失败即停止启动。
- 代理密码不通过账户 API 返回；优先存入系统凭据存储，不可用时保存到权限为 `0600` 的本地秘密文件。
- 关闭使用 `cloakbrowser>=0.5.8` 的完整清理逻辑，同时按 `userDataDir` 清理残留浏览器进程。
- 正式指纹验收建议在 Windows headed 环境进行；macOS 不作为 GPU 指纹生产环境。

## 安装

要求 **Python 3.12 或更高版本**。

Windows：

```powershell
py -3.12 -m venv .venv
```

macOS/Linux：

```bash
python3.12 -m venv .venv
```

Windows：

```powershell
.venv\Scripts\activate
python -m pip install --upgrade pip setuptools
pip install -e ".[dev]"
```

macOS/Linux：

```bash
source .venv/bin/activate
python -m pip install --upgrade pip setuptools
pip install -e ".[dev]"
```

首次使用可让 CloakBrowser 自动下载二进制：

```bash
python -c "from cloakbrowser.download import ensure_binary; print(ensure_binary())"
```

也可以把现有二进制目录或 `chrome.exe` 路径填入 Web 控制台的全局设置，或设置
`CLOAKBROWSER_BINARY_PATH`。

## 启动

```bash
cp config/accounts.example.json config/accounts.json
python --version  # 应为 3.12+
python -m browser_custom
```

打开 <http://127.0.0.1:8787/>。

如果从空配置开始，也可以不复制示例文件，直接在控制台中创建账户。

## 账户配置

主要字段：

| 字段 | 说明 |
|---|---|
| `acc` | 系统生成的不可变 ID，也是稳定指纹 seed 的来源 |
| `userDataDir` | 账户独占的 persistent profile 绝对路径 |
| `browserPath` | 可选的账户级 CloakBrowser 二进制或目录 |
| `network.proxy` | 结构化 SOCKS5/HTTP(S) 代理，服务器、用户名和密码分离 |
| `network.regionMode` | `auto` 自动匹配、`manual` 手动设置、`disabled` 关闭地区匹配 |
| `network.timezoneOverride` / `localeOverride` | 可选的时区、语言覆盖；未设置的值由出口 IP 自动匹配 |
| `network.strictProxy` | 代理或网络身份解析失败时禁止启动 |
| `network.lastCheck` | 最近一次只读检测快照，不作为下次启动的出口 IP 来源 |
| `geolocation` | 可选的网站 Geolocation API 定位，与代理 GeoIP 分开管理 |
| `releaseChannel` | CloakBrowser 二进制通道：`stable` 或 `preview` |
| `browserVersion` | 可选的真实 CloakBrowser 二进制精确版本锁定；留空自动解析通道版本 |
| `fpPlatform` | 高级平台 persona 覆盖：`auto`（推荐）、`windows` 或 `macos` |
| `platformVersion` | 高级 UA Client Hints `platformVersion` 覆盖；默认留空 |
| `brandVersion` | 高级 User-Agent / Client Hints 品牌版本覆盖；默认留空 |
| `cloakArgs` | 其他 CloakBrowser/Chromium 启动参数 |
| `humanize` / `humanPreset` | CloakBrowser 拟人操作开关及预设 |

浏览器运行中不能修改账户配置；先关闭，再修改并重新打开。

设备指纹默认使用“自动生成一致身份”。只有确实需要兼容特定测试环境时才展开
“高级指纹覆盖”；手动设置平台或 UA/CH 版本可能造成字体、GPU、平台信息和真实 Chromium
行为之间不一致。

## 插件管理

在全局设置的“全局插件目录”中每行填写一个绝对路径。每个目录必须是已解压的
Chrome 扩展，并直接包含 `manifest.json`。程序在打开账户时通过 CloakBrowser 官方
`extension_paths` 参数加载这些目录，无需逐个 profile 手动安装。

多个账户可以加载同一插件目录；插件代码共享，但 `chrome.storage.local`、Cookie、
IndexedDB、登录状态等数据仍保存在各账户独立的 `userDataDir` 中，彼此隔离。

更新插件时，替换原固定目录中的代码，再勾选相关账户点击“批量重启”。正式替换第三方
插件文件时，建议先关闭所有使用该插件的账户。插件目录应保持稳定，避免扩展 ID 和已有
配置发生变化。CloakBrowser 会限制为 `extensionPaths` 中列出的插件，因此应把需要使用的
所有已解压插件都加入列表。

## 网络与地区身份

推荐保持“自动匹配出口 IP”：程序把代理交给 CloakBrowser，并使用官方 `geoip=True`
在每次启动时重新获取当前出口 IP，再统一设置时区、语言和 WebRTC IP。轮换代理不会继续使用
上一次保存的出口 IP。

“检测网络并预览身份”使用与真实启动相同的 CloakBrowser GeoIP 解析链路，显示检测到的
出口 IP、时区、语言以及最终应用值。首次检测会下载约 70 MB 的 GeoLite2 City 数据库，
之后由 CloakBrowser 缓存和更新。

自动模式允许单独覆盖时区或语言；完全手动模式要求同时填写两项。时区使用 IANA 名称
（如 `Asia/Shanghai`），语言使用 BCP 47 代码（如 `zh-CN`）。即使存在覆盖值，程序仍会
实时解析代理出口 IP，用于 WebRTC 一致性。

旧版配置首次加载时会迁移到新的 `network` / `geolocation` 结构，并在同目录保留
`accounts.json.v1.bak`。旧的 `exitIp` 仅迁移为已过期检测记录；旧的
`--fingerprint-location` 不再使用。

代理密码优先保存在 macOS Keychain、Windows Credential Manager 等 `keyring` 后端。
如果操作系统没有可用后端，则回退到配置目录下权限为 `0600` 的 `secrets.json`。账户列表、
账户 API 和日志只显示脱敏后的代理地址。

以下参数由网络身份模块保留，不能写入 `cloakArgs`：

```text
--proxy-server
--fingerprint-timezone
--fingerprint-locale
--lang
--fingerprint-webrtc-ip
--fingerprint-location
```

## API

```text
GET    /api/health
GET    /api/settings
PUT    /api/settings
GET    /api/accounts
POST   /api/accounts
PUT    /api/accounts/{acc}
DELETE /api/accounts/{acc}
GET    /api/browser/status
POST   /api/browser/batch              # {"action":"open|close|restart","accounts":["acc-..."]}
POST   /api/browser/{acc}/open
POST   /api/browser/{acc}/close
POST   /api/browser/{acc}/restart
GET    /api/cloak/options
POST   /api/cloak/network-test
POST   /api/cloak/proxy-test          # 兼容旧客户端，返回同一组合检测结果
```

## 测试

```bash
pytest
```

单元测试不启动真实 CloakBrowser。Windows 真机验收还应覆盖：多账户并发、代理出口、指纹稳定性、
手工关闭后的状态更新，以及反复开关后 Chromium/Playwright 进程是否完全回收。
