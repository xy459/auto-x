# 可视化工作流引擎设计文档

> 状态：设计冻结，暂不实现
> 日期：2026-08-20
> 目的：记录已经确认的工作流引擎边界、数据模型与执行语义，供后续开发使用。

## 1. 背景与边界

整个系统已经确定两个基础模块。

### 1.1 browser-custom

负责：

- CloakBrowser 与 Playwright persistent context；
- 浏览器 Profile 和独立 userDataDir；
- 代理、GeoIP、语言、时区、WebRTC；
- 插件加载；
- 浏览器打开、关闭、重启和状态；
- 向执行层提供 BrowserSession、BrowserContext 和 Page。

不负责：

- X/Twitter 页面语义；
- 原子动作；
- 业务任务；
- 动作组编排；
- 多账户业务调度。

### 1.2 x-actions-playwright

负责：

- 使用 Python Playwright 实现 X/Twitter 原子动作；
- 页面类型、帖子、评论、账号和时间线识别；
- Locator、点击、输入、导航与后置状态验证；
- 统一动作输入、输出、错误和执行状态；
- 向业务层提供机器可读的动作目录。

不负责：

- 账户业务管理；
- 定时任务；
- 动作组编排；
- 多账户调度；
- AI 业务决策。

### 1.3 可视化工作流引擎

工作流引擎位于以上两个模块之上，让管理员组合原子动作、条件、循环、数据处理和 AI 节点，形成可复用动作组，并在创建任务时选择动作组或工作流执行。

当前阶段只形成设计文档，不实施工作流引擎。

## 2. 已确认原则

### 2.1 管理员决定执行内容

管理员在已发布动作组中配置了点赞、回复、Quote、关注等动作后，执行引擎应按照编排直接执行。

不设计：

- 写操作每日额度；
- 点赞或回复次数限制；
- 强制人工审批；
- 写动作前的复杂业务 Policy Gate；
- 系统自动否决管理员已经发布的动作。

人工审批可以作为可选节点存在，但不会由系统强制插入。

### 2.2 受约束只表示技术约束

必须保留：

- 输入输出类型校验；
- 动作存在性和启用状态检查；
- 同账户互斥；
- 同一个 Page 中浏览器动作顺序执行；
- 循环有明确边界；
- 节点和动作有超时；
- 发布版本不可修改；
- 任务固定绑定明确版本；
- 节点输入、输出和错误持久化；
- 写动作结果无法确认时返回 uncertain；
- 不对不可安全重试的动作盲目自动重试。

这些是执行正确性要求，不是业务额度或行为限制。

### 2.3 不支持任意代码节点

管理台不允许管理员输入任意 Python、JavaScript、Shell 或 eval 代码。

需要新能力时，通过新增受审核的节点类型扩展。

## 3. 总体层次

~~~mermaid
flowchart TD
    Catalog["原子动作目录 Action Catalog"] --> NodeLibrary["节点库 Node Library"]
    NodeLibrary --> GroupDraft["动作组草稿 Group Draft"]
    GroupDraft --> Compiler["动作组编译器"]
    Compiler --> GroupVersion["不可变动作组版本"]

    GroupVersion --> WorkflowDraft["工作流草稿"]
    WorkflowDraft --> WorkflowCompiler["工作流编译器"]
    WorkflowCompiler --> WorkflowVersion["不可变工作流版本"]

    GroupVersion --> TaskDefinition["任务定义"]
    WorkflowVersion --> TaskDefinition
    TaskDefinition --> TaskRun["任务实例"]
    TaskRun --> GroupRun["动作组实例"]
    GroupRun --> NodeRun["节点实例"]
    NodeRun --> Attempt["执行尝试"]
~~~

层次说明：

1. ActionDefinition：原子动作定义；
2. NodeDefinition：节点类型定义；
3. ActionGroup：管理员编排的可复用动作组；
4. Workflow：组合多个动作组；
5. TaskDefinition：账户、版本、参数和计划；
6. TaskRun：某个账户的一次实际执行；
7. GroupRun：某个动作组版本的一次执行；
8. NodeRun：某个节点的一次逻辑执行；
9. NodeAttempt：安全重试产生的单次尝试。

一个任务选择多个账户时，应为每个账户创建独立 TaskRun。

## 4. 动作组模型

动作组应被视为一个可版本化函数：

~~~text
ActionGroup(Input) -> Output
~~~

示例：

- 浏览 For you 并匹配指定作者；
- 打开目标帖子并点赞；
- 读取帖子并生成 AI 回复；
- 对匹配帖子依次点赞和收藏。

动作组内部实现可以改变，但只要公开输入输出兼容，引用方就不需要了解内部节点。

## 5. 原子动作目录

x-actions-playwright 应输出机器可读的动作定义，管理台依据定义自动生成节点和参数表单。

建议字段：

~~~python
class ActionDefinition(BaseModel):
    id: str
    version: str
    category: str
    title: str
    description: str

    input_schema: dict
    output_schema: dict

    access: Literal["read", "write"]
    retry_policy: Literal["safe", "never"]
    idempotent: bool
    enabled: bool

    timeout_default_ms: int
    timeout_max_ms: int

    requires_page_type: list[str]
    resulting_page_type: str | None

    statuses: list[str]
    failure_modes: list[FailureMode]
    edge_cases: list[str]
~~~

统一状态：

~~~text
success
skipped
navigating
uncertain
cancelled
failed
~~~

统一结果：

~~~json
{
  "status": "success",
  "action": "interaction.like",
  "category": "interaction",
  "data": {},
  "evidence": [],
  "warnings": [],
  "meta": {
    "durationMs": 1200,
    "access": "write",
    "retryPolicy": "never"
  }
}
~~~

## 6. 每个动作的输入输出

每个动作必须声明 input_schema 和 output_schema。

例如 timeline.collect 输入：

~~~json
{
  "type": "object",
  "required": ["feed"],
  "properties": {
    "feed": {
      "type": "string",
      "enum": ["for-you", "following"]
    },
    "maxScrolls": {
      "type": "integer",
      "minimum": 0,
      "maximum": 100,
      "default": 20
    },
    "intervalMs": {
      "type": "integer",
      "minimum": 250,
      "maximum": 10000,
      "default": 1500
    },
    "distance": {
      "type": "integer",
      "minimum": 200,
      "maximum": 3000,
      "default": 650
    },
    "maxPosts": {
      "type": "integer",
      "minimum": 1,
      "maximum": 1000,
      "default": 100
    },
    "includeAds": {
      "type": "boolean",
      "default": false
    }
  }
}
~~~

输出：

~~~json
{
  "status": "success",
  "data": {
    "feed": "for-you",
    "posts": [],
    "collectedCount": 20,
    "scrolls": 8,
    "stopReason": "max-posts",
    "checkpoint": {}
  }
}
~~~

interaction.reply 输入：

~~~json
{
  "tweetId": "123456789",
  "text": "回复内容"
}
~~~

成功输出：

~~~json
{
  "status": "success",
  "data": {
    "tweetId": "123456789",
    "createdReplyId": "987654321",
    "createdReplyUrl": "https://x.com/user/status/987654321"
  }
}
~~~

已点击发布但无法确认时：

~~~json
{
  "status": "uncertain",
  "data": {
    "tweetId": "123456789",
    "createdReplyId": null,
    "reason": "submission-result-not-confirmed"
  }
}
~~~

## 7. 节点类型

### 7.1 ActionNode

调用一个 x-actions-playwright 原子动作。

### 7.2 ConditionNode

二元条件节点，输出 true、false 和 error。

### 7.3 SwitchNode

多分支节点，例如根据 interactionType 进入点赞、回复或 Quote 分支。

### 7.4 ForEachNode

遍历数组，必须设置 maxItems。

同一账户、同一 Page 中的子流程默认顺序执行。

### 7.5 MatchRuleNode

为管理台提供内容匹配能力。

支持字段：

- author.handle；
- author.platformUserId；
- author.displayName；
- postId；
- text；
- language；
- type；
- isReply；
- isQuote；
- isRepost；
- media.imageCount；
- media.videoCount；
- metrics.likeCount；
- metrics.replyCount；
- metrics.repostCount；
- metrics.viewCount。

输出应包含 matched、route 和 matchedRules，便于解释为什么进入某个动作分支。

### 7.6 WaitNode

等待有限时间。

### 7.7 SetVariableNode

设置固定值或保存节点输出。

### 7.8 TransformNode

提供受控数据处理：

- filter；
- map；
- unique；
- slice；
- sort；
- pick；
- merge。

### 7.9 AIGenerateNode

输入帖子内容、语言、提示词模板等信息，输出生成文本和模型元数据。

AI 节点可以直接连接 interaction.reply 或 interaction.quote。

### 7.10 OptionalApprovalNode

可选人工审批节点。只有管理员主动放入动作组时才生效。

### 7.11 GroupReferenceNode

引用另一个已发布动作组的明确版本。

### 7.12 OutputNode

声明动作组对外输出。

## 8. 匹配表达式

规则支持 all、any 和 not 组合。

字符串操作符：

~~~text
equals
equals_ignore_case
not_equals
contains
contains_ignore_case
starts_with
ends_with
regex
is_empty
is_not_empty
~~~

列表操作符：

~~~text
in
not_in
in_ignore_case
contains_any
contains_all
~~~

数字操作符：

~~~text
equals
greater_than
greater_than_or_equal
less_than
less_than_or_equal
between
~~~

布尔和存在性操作符：

~~~text
is_true
is_false
exists
not_exists
~~~

正则表达式在发布前验证，并采用有执行时间限制的安全匹配实现。

匹配模式：

- first_match：按优先级执行第一个命中分支；
- all_matches：执行全部命中分支；
- 后续可增加 merge_actions。

## 9. 动作组输入输出

动作组输入和输出使用 JSON Schema 或等价类型系统。

示例输入：

~~~json
{
  "type": "object",
  "required": ["feed", "targetAuthor"],
  "properties": {
    "feed": {
      "type": "string",
      "enum": ["for-you", "following"]
    },
    "targetAuthor": {
      "type": "string"
    },
    "maxScrolls": {
      "type": "integer",
      "default": 10
    },
    "intervalMs": {
      "type": "integer",
      "default": 1500
    },
    "maxPosts": {
      "type": "integer",
      "default": 50
    }
  }
}
~~~

示例输出：

~~~json
{
  "type": "object",
  "properties": {
    "matchedPosts": {
      "type": "array",
      "items": {
        "$ref": "#/$defs/Post"
      }
    },
    "collectedCount": {
      "type": "integer"
    },
    "matchedCount": {
      "type": "integer"
    }
  }
}
~~~

外部任务和工作流只能读取动作组声明的输出，不能直接依赖内部任意节点变量。

## 10. 变量和绑定

运行时作用域：

~~~text
task
account
group.input
group.vars
nodes
loop
~~~

示例：

~~~text
task.id
account.browserProfileId
group.input.targetAuthor
group.vars.replyText
nodes.collect_timeline.output.data.posts
loop.post
loop.index
~~~

内部 Binding：

~~~json
{
  "source": "node_output",
  "nodeId": "collect_timeline",
  "path": "data.posts"
}
~~~

管理台使用变量选择器，不要求管理员手写复杂路径。

## 11. 编排模型

底层使用有向图：

~~~text
nodes
edges
~~~

边绑定来源端口：

~~~json
{
  "fromNode": "match_author",
  "fromPort": "true",
  "toNode": "open_post"
}
~~~

普通动作结果端口：

~~~text
success
skipped
navigating
uncertain
failed
cancelled
~~~

第一版不支持自由回边。循环只能通过 ForEach 表达。

## 12. 典型匹配与动作编排

~~~mermaid
flowchart TD
    Collect["浏览并采集 For you"] --> Each["逐条遍历帖子"]
    Each --> MatchID{"帖子 ID 命中？"}
    MatchID -- 是 --> IDActions["打开 → 点赞 → 收藏"]
    MatchID -- 否 --> MatchKOL{"作者属于目标 KOL？"}
    MatchKOL -- 是 --> KOLActions["打开 → 点赞 → AI 回复"]
    MatchKOL -- 否 --> MatchText{"正文命中关键词？"}
    MatchText -- 是 --> TextActions["打开 → 点赞"]
    MatchText -- 否 --> Next["继续下一条"]
    IDActions --> Next
    KOLActions --> Next
    TextActions --> Next
~~~

时间线使用虚拟列表。推荐：

~~~text
先采集
→ 再过滤和匹配
→ 使用 postId/postUrl 打开详情
→ 再执行点赞、回复或 Quote
~~~

## 13. 编译与校验

发布流程：

~~~mermaid
flowchart LR
    Draft["草稿"] --> Normalize["规范化"]
    Normalize --> Structure["结构校验"]
    Structure --> Resolve["动作与依赖解析"]
    Resolve --> Types["输入输出类型检查"]
    Types --> Control["控制流检查"]
    Control --> Bounds["运行边界检查"]
    Bounds --> Plan["Compiled Plan"]
    Plan --> Hash["Checksum"]
    Hash --> Publish["发布不可变版本"]
~~~

### 13.1 结构校验

- 节点 ID 唯一；
- 边引用节点存在；
- 入口和 OutputNode 存在；
- 没有悬空或不可达节点；
- 没有普通循环边；
- ForEach 声明 maxItems；
- GroupReference 不构成递归。

### 13.2 动作与依赖解析

- actionId 存在并启用；
- 引用动作组版本存在且已发布；
- 所有依赖绑定明确版本；
- 嵌套深度有限。

### 13.3 类型检查

- 必填输入完整；
- 固定值符合 Schema；
- Binding 路径存在；
- 输出类型满足下一步输入；
- Condition 两边类型可比较；
- AI 文本输出可以传给回复或 Quote 的 text 字段。

### 13.4 控制流检查

- Condition 的 true/false 分支完整；
- Switch 有 default 或未命中策略；
- 节点最终可到达 Output；
- ForEach 子流程可以结束；
- failed 和 uncertain 有默认语义。

### 13.5 运行边界检查

- Wait 有限；
- ForEach 有界；
- 动作和节点有超时；
- 同一个 Page 上浏览器动作不并行；
- 动作组引用不会无限嵌套。

编译不会检查写操作额度，也不会自动插入审批。

### 13.6 Compiled Plan

Plan 保存：

- 动作组版本；
- 动作库版本；
- Catalog Hash；
- 规范化节点和边；
- 已解析依赖；
- 输入输出 Schema；
- 错误策略；
- 编译器版本；
- Checksum。

任务执行 Compiled Plan，而不是管理台草稿。

## 14. 执行模型

执行过程：

1. Worker 领取 TaskRun；
2. 获取账户互斥租约；
3. 获取全局浏览器槽位；
4. 通过 browser-custom 获取 BrowserSession 和 Page；
5. 创建 GroupRun；
6. 根据 Compiled Plan 执行 ready 节点；
7. 节点执行前保存输入和 running 状态；
8. 调用 x-actions-playwright 或节点执行器；
9. 保存输出、错误、截图和耗时；
10. 根据结果端口选择下一条边；
11. 保存动作组输出；
12. 释放浏览器会话、槽位和账户租约。

同一个账户和 Page 中的浏览器动作严格顺序执行。

### 14.1 导航

动作返回 navigating 且 requiresRetry=true 时：

1. 保存第一次动作结果；
2. 等待页面加载；
3. 再次执行当前节点；
4. 限制导航阶段次数。

导航阶段不视为普通错误重试。

### 14.2 ForEach

每次迭代保存：

~~~text
foreach_node_id
iteration_index
item_key
item_snapshot
status
~~~

任务中断后可以从准确位置恢复。

## 15. 错误策略

支持：

~~~text
retry
continue
goto
skip
stop
fail
cancel
fallback_group
~~~

示例：

~~~json
{
  "onError": [
    {
      "codes": ["TIMEOUT", "ELEMENT_BLOCKED"],
      "whenRetryable": true,
      "strategy": "retry",
      "maxAttempts": 2,
      "delayMs": 1000
    },
    {
      "codes": ["TARGET_NOT_FOUND"],
      "strategy": "continue",
      "nextNodeId": "next_post"
    },
    {
      "codes": ["*"],
      "strategy": "fail"
    }
  ]
}
~~~

retry 只适用于动作目录标记为 retryPolicy=safe 的动作。

uncertain 可配置：

- stop；
- continue；
- goto verification node；
- mark task partial。

## 16. 持久化模型

主要表：

~~~text
action_catalog_snapshots
action_groups
action_group_drafts
action_group_versions
workflows
workflow_drafts
workflow_versions
task_definitions
task_runs
group_runs
node_runs
node_attempts
loop_iteration_runs
run_events
artifacts
~~~

### 16.1 动作目录快照

保存动作库版本、Catalog Hash 和完整定义。

### 16.2 动作组草稿

保存 Graph、输入输出 Schema 和最近校验结果，可以覆盖更新。

### 16.3 动作组版本

保存不可变 Graph Snapshot、Compiled Plan、Compiler Version、Catalog Snapshot、Checksum 和发布信息。

### 16.4 TaskRun

保存：

- 绑定版本；
- Compiled Plan Snapshot；
- 输入快照；
- 账户快照；
- 当前节点；
- Worker；
- 状态；
- 结果和错误摘要。

### 16.5 NodeRun 和 NodeAttempt

保存：

- 解析后的输入；
- 输出；
- 错误；
- 循环路径；
- 尝试次数；
- 截图；
- Trace；
- 执行耗时。

## 17. 版本管理

版本使用递增整数：

~~~text
v1
v2
v3
~~~

生命周期：

~~~text
draft
→ validated
→ published
→ deprecated
→ archived
~~~

规则：

1. 草稿可以修改；
2. 发布创建新的不可变版本；
3. 已发布版本不能原地修改；
4. 修改历史版本需要从该版本创建新草稿；
5. 任务绑定明确版本；
6. TaskRun 保存 Compiled Plan 快照；
7. Deprecated 版本不再推荐给新任务；
8. Archived 版本仍保留历史引用；
9. 回滚只修改推荐版本指针；
10. GroupReference 固定引用明确版本。

## 18. 运行恢复

- 未开始节点：直接执行；
- 安全读动作中断：根据错误策略重新执行；
- 已成功节点：复用持久化输出；
- 写动作中断且结果不可靠：标记 uncertain；
- 已成功 AI 节点：默认复用原输出；
- ForEach 中断：从未完成迭代恢复；
- 账户或浏览器异常：保存当前位置后交给恢复策略。

## 19. 管理台

### 19.1 原子动作目录

展示分类、输入、输出、读写属性、状态、错误、超时和重试语义。

### 19.2 动作组编辑器

布局：

~~~text
左侧：节点库
中间：编排画布
右侧：节点配置
底部：编译结果
~~~

### 19.3 匹配规则编辑器

管理员通过字段、操作符和值配置匹配，支持优先级、first_match 和 all_matches。

### 19.4 编译面板

显示：

- 错误；
- 警告；
- 节点和分支数量；
- 最大循环数量；
- 读写动作摘要；
- 动作组依赖；
- Catalog 和编译器版本。

写动作摘要只展示，不阻止发布。

### 19.5 版本页面

提供：

- 草稿和发布版本；
- 发布人和时间；
- 版本差异；
- 依赖关系；
- 从历史版本创建草稿；
- Deprecated；
- 推荐版本切换。

### 19.6 TaskRun 详情

颜色：

~~~text
灰色：未执行
蓝色：运行中
绿色：成功
浅绿色：跳过
黄色：uncertain
红色：失败
~~~

节点详情展示输入、输出、错误、尝试次数、截图、Trace、循环索引和耗时。

## 20. 运行重现和重跑

不能保证 X 外部页面状态完全重现，但应保证执行配置可重现。

TaskRun 保存：

- 动作组或工作流版本；
- Compiled Plan；
- 输入和账户快照；
- 动作库版本；
- AI 模型和提示词版本；
- 每一步输入输出；
- 截图和 Trace。

支持：

1. 克隆为新任务；
2. 使用原版本和原输入完整重跑；
3. 从失败节点创建新的重跑任务；
4. AI 节点选择复用原输出或重新生成；
5. 明确展示重跑会再次执行的写动作。

历史 TaskRun 永远不被重跑操作修改。

## 21. 示例动作组

输入：

~~~json
{
  "feed": "for-you",
  "maxScrolls": 10,
  "intervalMs": 1500,
  "maxPosts": 50,
  "kolHandles": ["openai", "github"],
  "targetPostIds": ["123456789"],
  "keywords": ["AI", "Python", "Playwright"],
  "replyMode": "ai",
  "fixedReply": null
}
~~~

编排：

~~~text
account.getSession
→ timeline.collect
→ ForEach posts
    → 匹配精确帖子 ID
        → post.openDetails
        → interaction.like
        → interaction.bookmark
    → 否则匹配目标 KOL
        → post.openDetails
        → interaction.like
        → post.getDetails
        → ai.generateReply
        → interaction.reply
    → 否则匹配正文关键词
        → post.openDetails
        → interaction.like
    → 否则继续下一条
→ Output
~~~

输出：

~~~json
{
  "collectedCount": 50,
  "matchedCount": 4,
  "actionResults": [
    {
      "postId": "123456789",
      "actions": ["like", "bookmark"],
      "status": "success"
    }
  ]
}
~~~

## 22. 暂缓实施与未来顺序

当前阶段不实现工作流引擎。

未来建议顺序：

1. 等 x-actions-playwright 的动作目录、输入输出和错误契约稳定；
2. 定义 Action Catalog Snapshot；
3. 实现动作组 Draft 和 Version 数据模型；
4. 实现编译器，先不做可视化界面；
5. 实现最小执行引擎和 NodeRun 持久化；
6. 使用 JSON 动作组跑通端到端；
7. 最后开发可视化编排器；
8. 再增加 Workflow 组合、AI 节点和运行重现。

这样可以避免在原子动作契约尚未稳定时，过早投入复杂可视化编辑器。
