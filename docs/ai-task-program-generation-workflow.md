# AI 生成任务程序工作流

> 状态：建议作为后续 AI 生成 Task Program 的工作规范
> 日期：2026-08-20
> 适用范围：用户用自然语言描述 X/Twitter 任务流程，由 AI 在当前仓库生成或修改 Python 任务程序
> 关联文档：[脚本式任务系统架构设计](script-task-system-architecture.md) · [Task Runner 详细处理流程](task-runner-processing-design.md)

## 1. 目标

用户可以直接描述业务流程，例如：

> 浏览 Following 时间线，匹配包含 Python 的帖子，先点赞，再用 AI 生成简短回复；最多处理 5 条，任何写操作结果无法确认时立即停止。

AI 将这段描述转换为：

1. 可审查的任务规格；
2. 符合现有架构边界的 Task Program；
3. 对应的参数模型、注册信息和测试；
4. 静态检查与测试结果；
5. 明确的假设、风险和未实现能力。

生成工作流只修改受 Git 管理的源码，不直接在真实账户上执行任务，也不把 AI 输出作为任意代码交给后台动态运行。

## 2. 核心原则

### 2.1 先形成规格，再生成代码

AI 不应从一段模糊描述直接跳到 Python。必须先整理成稳定的中间规格，确认以下内容：

- 从哪里获取目标；
- 如何筛选目标；
- 动作及其顺序；
- 循环和停止条件；
- 是否以及如何调用 AI；
- 写操作的数量上限；
- `skipped`、`failed`、`uncertain` 和取消状态如何处理；
- 最终输出哪些统计值。

存在会改变真实外部行为的歧义时，必须先向用户确认，不能自行选择一个写操作策略。

### 2.2 生成的是源码变更，不是运行时工作流

生成结果继续遵守当前“每种任务一个 Python 任务程序”的架构：

```text
自然语言描述
  → 任务生成规格
  → Python Task Program
  → 单元测试和静态检查
  → 人工审查 Git diff
  → 提交和部署
  → 由 Task Runner 创建并执行 TaskRun
```

禁止把生成结果直接写入数据库并动态 `eval`、`exec`、导入或执行。

### 2.3 只组合已经存在的公共能力

Task Program 只能通过 `TaskContext` 使用：

- `context.account`
- `context.actions`
- `context.ai`
- `context.logger`
- `context.cancellation`

X 页面操作必须映射到 `x-actions-playwright` 已注册动作。若用户需要的动作不存在，生成流程应停止，并将需求拆成“先扩展动作库，再生成任务程序”两个独立改动。

## 3. 任务生成中间规格

AI 在写代码前应先整理出以下规格。它是审查材料，不是新的运行时工作流格式。

```yaml
identity:
  name: like_and_reply_python_posts
  title: 点赞并回复 Python 帖子
  description: 浏览指定时间线，匹配帖子后依次点赞和回复

source:
  type: timeline
  feed: following
  max_scrolls: 20
  scroll_interval_seconds: 1.5
  scroll_distance: 650

selection:
  authors: []
  keywords: [Python]
  include_ads: false
  deduplicate_by: post_id

steps:
  - action: interaction.like
  - action: ai.generate
    template: reply_to_post
    variables: [post_text, author]
  - action: interaction.reply

limits:
  max_targets: 5
  max_write_actions: 10

result_policy:
  skipped: continue
  failed: stop
  uncertain: stop_and_propagate
  cancelled: stop

output:
  - posts_seen
  - matched
  - liked
  - replied
  - skipped
```

### 3.1 必填信息

涉及写操作时，下列信息必须明确：

- 写什么：点赞、回复、转发、关注、发帖等；
- 对谁写：目标来源与筛选条件；
- 写多少：目标数量或写动作数量上限；
- 动作顺序：例如先点赞再回复，还是回复成功后再点赞；
- 写操作失败时是停止还是跳过；
- 写操作结果不确定时必须停止并传播，不得默认重试；
- 回复、引用或发帖的内容来自固定文本还是 AI。

### 3.2 可以安全补全的信息

不改变业务含义时，AI 可以自行补全：

- 结构化日志字段；
- JSON 输出字段名称；
- 内部计数变量名称；
- 测试夹具和假动作实现；
- 代码格式和导入顺序。

### 3.3 必须询问用户的信息

以下歧义不得靠猜测解决：

- 是否执行真实写操作；
- 目标账户、作者、关键词或时间线范围；
- 删除、取消关注、发送私信、发帖等高影响动作；
- 固定回复的实际内容或 AI 模板；
- 没有数量上限的循环或批量写入；
- 多个动作的先后顺序；
- 单个动作失败后继续还是终止；
- 是否允许处理广告、自己的帖子、回复、引用帖或敏感目标；
- 用户要求的能力在动作库中不存在时，是否扩展底层动作库。

## 4. 生成流程

### 阶段 1：理解请求

AI 将用户描述拆成：

- 任务目标；
- 输入参数；
- 数据来源；
- 筛选规则；
- 动作序列；
- 循环和停止条件；
- AI 使用方式；
- 输出和日志；
- 异常处理策略。

如果请求与现有程序高度一致，应优先建议复用或扩展现有参数，避免创建语义重复的任务程序。

### 阶段 2：边界和能力审查

逐项判断需求属于哪一层：

| 需求 | 所属模块 | 生成规则 |
|---|---|---|
| 匹配、顺序、循环、统计 | Task Program | 可以生成 |
| 日志、AI、取消、账户快照 | Task SDK | 只能调用现有接口 |
| TaskRun 状态、锁、并发、资源清理 | Task Runner | 不进入任务程序 |
| 点击、输入、Locator、页面状态确认 | `x-actions-playwright` | 只能调用已有动作 |
| 浏览器、Profile、代理、指纹 | `browser-custom` | 不进入任务程序 |
| 多账户拆分和定时触发 | API / Scheduler | 不进入单账户任务程序 |

若一个需求跨层，必须拆分实现，不能让任务程序越过公共接口直接完成。

### 阶段 3：风险分级

| 等级 | 类型 | 示例 | 生成要求 |
|---|---|---|---|
| R0 | 只读 | 浏览、收集、读取详情 | 有界循环、取消检查 |
| R1 | 幂等状态写入 | 点赞、收藏、关注 | 数量上限、稳定幂等键、后置状态验证 |
| R2 | 非幂等内容写入 | 回复、引用、发帖、私信 | 明确内容来源、数量上限、`uncertain` 停止、禁止自动重试 |
| R3 | 删除或关系破坏 | 删除帖子、取消关注 | 用户必须明确要求，单独说明目标和影响 |

风险等级不会改变动作库的最终判断，但决定生成规格必须包含哪些保护条件。

### 阶段 4：展示生成规格

写代码前，AI 应向用户展示简洁规格：

```text
程序：like_and_reply_python_posts
来源：Following 时间线，最多滚动 20 次
匹配：正文包含 Python，排除广告
动作：点赞 → AI 生成回复 → 发送回复
上限：最多 5 个目标、10 次写操作
失败：普通失败终止；skipped 继续；uncertain 立即停止
输出：浏览数、匹配数、点赞数、回复数、跳过数
```

若用户描述已经完整且没有高影响歧义，可以直接生成；否则等待用户补充关键选项。

### 阶段 5：生成代码

标准变更集合为：

```text
apps/x-ops/src/x_ops/task_programs/<program_name>.py
apps/x-ops/src/x_ops/task_programs/registry.py
apps/x-ops/tests/test_task_programs.py
```

按需要额外修改：

- `_common.py`：仅加入多个任务都能复用的纯业务辅助函数；
- AI 模板配置：任务确实需要新的模板时；
- 架构或使用文档：出现新的公共模式或参数语义时。

禁止为了单个任务方便而扩大 Task SDK 或把业务逻辑塞入 Runner。

### 阶段 6：自动审查生成代码

生成后必须检查：

1. 是否只调用允许的 `TaskContext` 能力；
2. 是否使用动作目录中真实存在且未废弃的动作；
3. 所有循环、滚动、目标数量和写动作是否有上限；
4. 是否在入口、循环、AI 调用和写动作边界检查取消；
5. 写操作是否使用稳定的幂等键；
6. `uncertain` 是否通过 `require_certain()` 传播；
7. 是否错误地把 `skipped` 计为成功；
8. 是否在虚拟时间线中先互动再滚动；
9. 输出是否为稳定、可 JSON 序列化的字典；
10. 日志是否避免凭据、Cookie、代理密码、完整提示词和不必要的帖子正文。

### 阶段 7：验证

至少执行：

```bash
cd apps/x-ops
python3.12 -m pytest tests/test_task_programs.py
ruff check src/x_ops/task_programs tests/test_task_programs.py
mypy src
```

根据改动范围补充 Runner、动作库或集成测试。若工具或依赖缺失，必须明确报告未执行项，不能声称验证通过。

### 阶段 8：交付和发布

AI 交付时应说明：

- 生成或修改了哪个程序；
- 参数和默认值；
- 调用的读取与写入动作；
- 停止条件和异常策略；
- 新增测试及验证结果；
- 已知限制、假设和风险；
- 是否更新了注册表和程序版本。

提交和部署后，程序才会进入任务程序目录。生成代码本身不创建 TaskRun，也不自动执行真实账户操作。

## 5. 代码生成约束

### 5.1 固定程序契约

每个任务程序必须导出：

```python
SPEC: ProgramSpec
Params: type[BaseModel]
async def run(context: TaskContext, params: Params) -> dict[str, Any]
```

要求：

- `SPEC.name` 使用唯一的 `snake_case` 名称；
- 新程序从 `1.0.0` 开始；
- 参数或业务含义变化时按兼容性提升版本；
- `Params` 为所有数字、文本和集合设置合理边界；
- `run()` 只返回 JSON 可序列化数据。

### 5.2 允许的依赖

任务程序原则上只允许导入：

- Python 标准库中的纯数据和类型工具；
- Pydantic；
- `TaskContext` 和 Task SDK 异常；
- `task_programs._common` 中的公共辅助函数；
- `ProgramSpec`。

### 5.3 禁止的依赖和行为

任务程序不得：

- 直接导入 Playwright 或编写 Locator；
- 自行创建、关闭或切换浏览器、Context、Page；
- 读取 Cookie、代理、指纹、API Key 或密钥文件；
- 直接访问数据库、TaskRun Store 或修改运行状态；
- 使用 `requests`、`httpx`、Socket 绕过 Task SDK 访问网络；
- 调用 Shell、子进程或操作系统自动化；
- 使用 `eval`、`exec`、动态导入或执行用户提供的代码；
- 枚举或调度其他账户；
- 在任务程序中实现定时调度、并发槽位或账户锁；
- 对结果不确定的写操作自动重试；
- 创建无边界循环或无数量上限的批量写操作。

### 5.4 取消约束

至少在以下位置调用：

```python
await context.cancellation.raise_if_cancelled()
```

- `run()` 入口；
- 每次循环开始；
- 每个写动作之前；
- AI 调用前后；
- 多阶段任务切换处。

等待必须使用：

```python
await context.cancellation.sleep(seconds)
```

不得使用不可取消的长时间 `asyncio.sleep()`。

### 5.5 时间线约束

只浏览时优先使用 `run_timeline()`；需要对帖子互动时优先使用 `run_timeline_batches()`。

互动任务必须：

- 收集当前可见批次；
- 按帖子 ID 去重；
- 排除广告，除非用户明确要求；
- 在目标帖子仍位于 DOM 时完成互动；
- 互动完成后再滚动；
- 页面到底或无法继续滚动时正常结束。

### 5.6 写操作约束

每个写操作必须通过 `write_options()` 生成选项，并使用可重复计算的幂等键：

```python
options=write_options(
    context,
    f"<program-action>:{context.account.account_id}:{target_id}",
)
```

动作结果必须经过：

```python
result = require_certain(
    result,
    task_run_id=context.cancellation.task_run_id,
)
```

处理原则：

- `success`：确认成功后才能增加成功计数；
- `skipped`：目标已满足或幂等复用，不能计为新成功；
- `failed`：按规格停止或跳过；
- `uncertain`：停止并传播到 Runner；
- `cancelled`：立即传播取消状态；
- 写动作触发后不得因为超时或未知状态直接再执行一次。

### 5.7 AI 约束

任务程序只能通过 `context.ai.generate()` 调用 AI：

- 模板 ID 必须存在或随变更一起新增；
- 变量名称必须与模板一致；
- AI 返回内容应在发送前进行空值和长度约束；
- AI 调用失败不得退化成未经用户允许的固定内容；
- 不把凭据、Cookie、代理信息或完整账户配置传入提示词；
- AI 只生成内容，不决定账户、目标范围、写入上限和重试策略。

## 6. 测试要求

每个生成程序至少覆盖以下适用场景：

### 参数

- 默认参数可用；
- 数量、滚动、间隔和文本长度边界；
- 不允许的参数组合被 Pydantic 拒绝；
- 固定内容模式不能为空；
- 没有任何有效动作时拒绝运行。

### 业务流程

- 动作调用顺序与用户描述一致；
- 作者和关键词匹配正确；
- 未匹配帖子不触发写动作；
- 重复帖子只处理一次；
- 互动发生在滚动之前；
- 达到目标或写动作上限后停止；
- 页面到底或滚动为零时停止；
- 输出统计与实际动作一致。

### 取消和异常

- 入口取消不调用任何动作；
- 两个写动作之间取消不会触发后一个动作；
- AI 调用后取消不会继续发送；
- `uncertain` 传播为 `TaskUncertainError`；
- 已处于目标状态的 `skipped` 不计为成功；
- 写操作的幂等键包含账户和稳定目标标识。

### AI

- 固定模式不调用 AI；
- AI 模式传入正确模板和变量；
- 空响应、超长响应和 Provider 错误按规格处理。

## 7. 版本、注册和回滚

### 新程序

- 新建 `<program_name>.py`；
- `SPEC.version = "1.0.0"`；
- 加入 `TaskProgramRegistry.default()`；
- 添加测试；
- 确认管理 API 能展示参数 Schema。

### 修改现有程序

- 仅修复不改变外部契约的问题：提升补丁版本；
- 新增向后兼容参数或能力：提升次版本；
- 删除字段、改变默认行为或输出语义：提升主版本，并评估是否更适合创建新程序。

历史 TaskRun 固化程序版本和参数快照。回滚通过 Git 和部署版本完成，不通过数据库覆盖历史源码。

## 8. 生成失败和停止条件

遇到以下情况，AI 不应继续生成一个看似可运行的脚本：

- 请求的动作在动作目录中不存在；
- 需要直接操作 Locator 或浏览器配置才能实现；
- 用户未明确高影响写操作的目标和范围；
- 写操作没有任何数量、时间或目标边界；
- 无法设计稳定的目标标识或幂等键；
- 用户要求对 `uncertain` 自动重试；
- 用户要求任务程序读取密钥、Cookie 或跨账户数据；
- 需求必须依赖任意 Shell、任意 Python 或动态代码执行；
- 现有架构无法可靠判断动作是否完成。

此时应说明阻塞原因，并提出最小的前置改动或需要用户确认的选项。

## 9. 用户描述任务的推荐方式

用户不必填写固定表单，可以直接描述。为了减少确认轮次，建议尽量包含：

```text
任务名称：
从哪里找目标：
匹配条件：
按什么顺序执行动作：
固定内容或 AI 内容：
最多处理多少目标 / 执行多少写操作：
遇到 skipped、failed、uncertain 时怎么办：
最终希望看到哪些统计：
```

示例：

```text
创建一个任务程序，从 Following 时间线读取帖子，最多滚动 15 次。
只处理作者为 openai 或正文包含 codex 的非广告帖子。
每条帖子先点赞，再使用 reply_to_post 模板生成回复并发送。
最多处理 3 条帖子；已点赞可以继续回复；任何 uncertain 立即停止；
单条普通失败跳过并记录。返回浏览数、匹配数、点赞数、回复数和跳过数。
```

AI 应先返回整理后的规格和必要问题，再生成代码。用户明确说“按合理默认值直接生成”时，也不能替用户推断删除、发帖、私信、取消关注等高影响行为。

## 10. 完成标准

一个 AI 生成任务程序只有同时满足以下条件才算完成：

- 任务规格与用户描述一致；
- 代码未跨越 Task Program 边界；
- 只调用真实存在的动作；
- 所有循环和写操作都有边界；
- 取消、幂等和 `uncertain` 处理正确；
- 参数、动作顺序、停止条件和统计有测试；
- 注册表和版本已更新；
- 静态检查与测试已执行，或明确报告无法执行的原因；
- Git diff 中没有无关改动；
- 未经单独授权，没有运行真实账户任务、提交、推送或部署。
