# 设计文档（Design.md）

> 项目：多 Agent 协作代码流水线（编码 → 审查 → 修复）
> 基于 LangGraph 1.2

---

## 1. 背景与目标

### 1.1 要解决的问题

单 Agent 只能"审查已有代码"，但真实开发流程是：**写代码 → 审代码 → 修代码 → 再审**。
本项目把核心流水线拆成编码、审查、修复三个职责清晰的角色，并增加意图路由、问答与
多轮对话层；各角色通过共享状态与显式消息协作，并把执行过程落成可追踪、可续跑的记录。

### 1.2 设计目标

| 优先级 | 目标 | 落地方式 |
|---|---|---|
| P0 | 端到端跑通 | router → coder → reviewer → fixer → reviewer 闭环 |
| P0 | 不是只支持一种输入 | router 先判意图：要代码走流水线，其它提问走 responder |
| P0 | Agent 间状态共享 | 自定义 WorkflowState（代码、findings、计数、快照） |
| P0 | 多角色接入 | 三组独立模型配置 + 对话模型实例；router / responder 默认复用 coder，chat 默认复用 fixer |
| P1 | 条件分支与循环 | 两级条件边：route_after_router（code / chat）+ route_after_review（passed / fix / give_up） |
| P1 | Agent 间显式通信 | AgentMessage 消息总线（谁发给谁、发了什么） |
| P1 | 错误处理与恢复 | 节点级重试 + 工具降级 + 致命错误保护 + 运行记录续跑 |
| P1 | 可观测 | 节点日志 + status + 消息时间线 + JSON 运行记录 |
| P1 | 可扩展 | 新增 Agent = 加 prompt + 加 node + 加一条边 |
| P1 | 结论可收敛 | 阻断阈值：只有 critical / warning 触发修复，info 只记录不阻断 |

---

## 2. 总体架构

```text
┌───────────────────────────────────────────────────────┐
│                     cli_multi.py                      │
│   工作模式 REPL：对话 / 工具轨迹 / 提案确认 / 指令      │
└───────────────────────────┬───────────────────────────┘
                            │ build_workflow(entry=...)
┌───────────────────────────▼───────────────────────────┐
│                 multi_agent/                          │
│                                                       │
│  graph.py       图组装：节点 + 条件边 + 入口参数       │
│  runner.py      执行器：驱动图、采集事件、写记录       │
│  recorder.py    运行记录：时间线 + 快照 + 原子写       │
│  edges.py       两级路由函数 + 续跑推算                │
│  intent.py      意图识别：规则 → 模型 → 兜底           │
│  severity.py    严重级别与阻断阈值判定                 │
│  state.py       共享状态与消息/错误/快照结构           │
│  messages.py    消息总线                               │
│  conversation.py 对话层：记忆 + 工具 + 任务提案       │
│  resilience.py  重试 / 降级 / 错误标记                 │
│  text_utils.py  输出解析兜底                           │
│  llm_factory.py 三组流水线模型 + 对话模型               │
│  prompts/       五个角色的 SystemPrompt                │
│  nodes/         五个节点（工厂 + 纯函数）              │
└───────────────────────────┬───────────────────────────┘
                            │ 工具调用
┌───────────────────────────▼───────────────────────────┐
│                 tools/（复用原项目）                   │
│  ast_analyzer（analyze_code + 工具封装）               │
│  code_executor（execute_code 结构化 + 工具封装）       │
│  file_reader / sandbox_policy                          │
└───────────────────────────┬───────────────────────────┘
                            │
┌───────────────────────────▼───────────────────────────┐
│  config.py   模型配置 + 韧性参数 + 沙箱边界 + 记录目录  │
└───────────────────────────────────────────────────────┘
```

**分层原则**：

- 节点只关心"读什么状态、调哪个模型、写什么状态"，不关心图怎么连
- 图组装只做"加节点、加边"，不做业务逻辑
- 执行器只做"驱动 + 采集 + 落盘"，不关心渲染；CLI 只做渲染
- 工具层保持原项目的对外行为，额外提供结构化调用接口给节点内部使用
- 配置三层分组：coder / reviewer / fixer 各自独立

### 2.1 两层结构（工作模式）

CLI 现在是**多轮会话**，所以系统实际分成两层：

| | 对话层（前台） | 流水线层（后台） |
|---|---|---|
| 图 | `conversation.py`：`chat ⇄ tools`（ReAct） | `graph.py`：`coder → reviewer ⇄ fixer` |
| 状态 | `MessagesState`（消息列表） | `WorkflowState`（黑板） |
| 记忆 | `MemorySaver` + `thread_id`（进程内多轮） | 单次运行，结果落 `runs/*.json` |
| 模型 | mimo-v2.6-pro（支持 tool calling） | coder / reviewer / fixer 各自的模型 |
| 工具 | file_reader / code_executor / ast_analyzer / write_file | 节点内部调用，不暴露给模型 |

**两层怎么衔接**：

1. 用户随便聊；对话 Agent 该调工具就调（读文件 / 跑验证 / 写文件——写前必确认）
2. 需求聊清楚后，它调用 `propose_code_task` 提交**任务提案**（结构化：requirement + summary）
3. CLI 展示提案，等用户拍板（回车执行 / n 跳过 / 直接说话继续补充）
4. 确认后用 `entry=coder` 直接启动流水线——**不再让 router 二次判断**，
   因为用户已经明确说了这是代码任务
5. 流水线跑完，把结论 + findings + 最终代码以 `【系统】` 前缀注入对话历史，
   于是"再改一下""为什么这么写"都能接着问

**为什么对话层不复用 `router` / `responder`**：那两个节点是流水线图内部的
一次性分流（给程序化调用与离线演示用）。工作模式要的是跨轮记忆 + 工具 +
提案确认，用 `MessagesState` 那套更干净，也避免把两种职责塞进同一张图。

---

## 3. 图设计

### 3.1 节点

| 节点 | 输入状态 | 输出状态 | 调用工具 |
|---|---|---|---|
| `router` | `requirement`、可选 `mode` | `mode` | 无（规则拿不准时才叫模型） |
| `responder` | `requirement` | `answer` | 无 |
| `coder` | `requirement` | `current_code`、`code_history` | 无 |
| `reviewer` | `current_code`、`self_check` | `findings`、`review_history` | ast_analyzer、code_executor |
| `fixer` | `current_code` + `findings` | `current_code`（更新）、`self_check`、`iteration+1` | code_executor（自验） |

节点不通过 LangChain 的 tool-calling 机制驱动工具，而是节点自己先跑工具、
再把结果作为上下文交给模型。原因：

- 不要求三个模型都支持 function calling（不同厂商支持度不一）
- 工具执行结果可被记录、可被降级，失败路径完全可控
- 模型只负责"判断"，职责更单一，输出更稳定

### 3.2 边

| 边 | 类型 | 说明 |
|---|---|---|
| START → entry | 普通边 | entry 默认 `router`，续跑时可指向任意节点 |
| router → 条件边 | 条件边 | 按 `mode` 分流：code → coder，chat → responder |
| responder → END | 普通边 | 问答结束，不进入审查 / 修复 |
| coder → reviewer | 普通边 | 编码完立刻审查 |
| reviewer → 条件边 | 条件边 | 根据阻断 findings / iteration / errors 路由 |
| fixer → reviewer | 普通边 | 修完回到审查，形成闭环 |

### 3.3 条件边（入口分支 + 审后分支）

入口分支只读 `mode`，不做业务判断：

```python
def route_after_router(state) -> Literal["code", "chat"]:
    return "chat" if state.get("mode") == "chat" else "code"
```

审后分支：

```python
def route_after_review(state) -> Literal["passed", "fix", "give_up"]:
    if has_fatal_error(state["errors"]) or state["status"] == "failed":
        return "give_up"                # 致命错误：绝不误判为"审查通过"
    if not blocking_findings(state["findings"]):
        return "passed"                 # 只剩 info 级建议 → 算通过
    if state["iteration"] >= state["max_iterations"]:
        return "give_up"                # 修太多轮还没好 → END（带剩余阻断问题）
    return "fix"                        # 还有阻断问题且没超轮次 → fixer
```

**为什么需要阻断阈值**：这是让流程“能收敛”的关键。模型审查天然会持续产出 style nit，
实测两轮下来问题数还会从 2 涨到 3（fixer 新加功能后反而多了可挑的地方）；
如果“有 findings 就不算通过”，流程永远到不了通过。现在把 findings 分两类：

- **阻断**（默认 critical / warning）：触发修复循环
- **非阻断**（默认 info）：完整记录进 findings 与运行记录，但不挡流程

阈值由 `WORKFLOW_BLOCKING_SEVERITIES` 配置。语义因此变清晰：
**通过 = 无阻断问题，而不是零建议**。未知级别一律当 warning——宁可多修一轮，
也不要因为模型写了 `high` 就被静默放过。

**为什么致命错误判断放在最前面**：早期版本里 coder 挂掉后 `current_code` 为空、
reviewer 直接返回空 findings，条件边就把它当成"审查通过"——这是最危险的
误判类型（把失败当成功）。现在 `errors` 里的 fatal 标记是终止信号的第一优先级。

**为什么用字符串分支而不是布尔值**：分支语义显式写在映射表里，
将来加分支（例如插入 `security_review` 节点）只需要往映射表加一项。

---

## 4. 状态设计

### 4.1 为什么不用 MessagesState

原项目用内置 `MessagesState`（纯消息列表），因为单 Agent 的核心是"对话 + 工具调用"。
但多 Agent 场景下，消息列表会把不同角色的对话混在一起：

- coder 的生成结果和 reviewer 的审查意见缠在同一条消息里
- fixer 无法稳定消费"问题清单"这个结构化数据
- 条件边无法直接从消息里判断"有没有问题"

所以改用显式字段的黑板（`WorkflowState`），**同时**保留一份结构化消息流用于可观测。

### 4.2 WorkflowState 字段

列表字段统一用 `Annotated[list, operator.add]` 做累加 reducer，
保证多个节点写入不会互相覆盖（LangGraph 的 channel 语义）。

| 字段 | 语义 | reducer |
|---|---|---|
| `requirement` | 原始输入（需求或提问） | 覆盖 |
| `mode` | `code` / `chat`（入口路由依据） | 覆盖 |
| `answer` | 问答分支的回答 | 覆盖 |
| `run_id` | 运行短 ID | 覆盖 |
| `current_code` | 当前代码黑板 | 覆盖 |
| `code_history` | 每版代码快照 | 累加 |
| `findings` | 结构化问题清单 | 覆盖 |
| `review_history` | 每轮 severity 统计 | 累加 |
| `self_check` | fixer 自验结果 | 覆盖 |
| `iteration` / `max_iterations` | 循环控制 | 覆盖 |
| `messages` | Agent 消息时间线 | 累加 |
| `errors` | 降级/致命错误 | 累加 |
| `status` / `log` | 阶段标签 / 日志 | 覆盖 / 累加 |

### 4.3 消息总线（Agent 间通信）

状态回答"数据在哪"，消息回答"谁对谁说了什么"。两者互补：
条件边只读状态（保证路由确定），人和运行记录读消息（保证过程可解释）。

```python
class AgentMessage(TypedDict, total=False):
    id: str                  # 短 ID，便于日志引用
    sender: str              # coder / reviewer / fixer / system
    recipient: str           # 下游 Agent，或 workflow / tools
    kind: str                # handoff / request_fix / verdict / report / error
    summary: str             # 一句话说明
    iteration: int
    created_at: str
    payload: dict            # 结构化附加信息（如 severity 分布）
```

**关键约束：消息不参与路由**。这样即使某条消息缺失，流程也不会走错；
消息层可以独立演进（例如以后汇总成 Markdown 报告）而不影响控制流。

### 4.4 结构化错误

```python
class WorkflowError(TypedDict, total=False):
    scope: str        # 出错的节点
    kind: str         # tool_error / llm_error / invalid_input / self_check
    message: str
    fatal: bool       # True = 终止流程；False = 已降级，流程继续
    created_at: str
```

`fatal` 这个布尔位是"降级"与"失败"的分界线，也是条件边唯一的终止依据之一。

---

## 5. 多模型接入

### 5.1 为什么三个独立 LLM 实例

三个模型走不同的 base_url、不同的 API Key、不同的 model 名。如果用同一个
ChatOpenAI 实例换参数，会有线程安全问题，也浪费连接池。

```python
@lru_cache(maxsize=1)
def get_coder_llm(): ...      # coder
@lru_cache(maxsize=1)
def get_reviewer_llm(): ...   # reviewer
@lru_cache(maxsize=1)
def get_fixer_llm(): ...      # fixer
```

### 5.2 依赖注入不再靠改全局

早期实现的 `build_workflow(coder_llm=...)` 是**改写 `llm_factory` 的模块属性**
（`llm_factory.get_coder_llm = lambda: fake`），一次 build 会污染后续所有 build，
测试之间互相串味、同进程跑两个 workflow 会互相踩。

现在改成节点工厂：

```python
def build_workflow(coder_llm=None, reviewer_llm=None, fixer_llm=None, entry="coder"):
    workflow.add_node("coder", make_coder_node(coder_llm))       # 闭包持有注入的模型
    workflow.add_node("reviewer", make_reviewer_node(reviewer_llm))
    workflow.add_node("fixer", make_fixer_node(fixer_llm))
```

`make_xxx_node(llm)` 在 `llm is None` 时才去工厂取实例，所以线上仍复用连接池，
测试可以注入假模型，互不干扰。

### 5.3 配置分组

`.env` 里三组独立前缀：`CODER_*`、`REVIEWER_*`、`FIXER_*`。
`ROUTER_*` / `RESPONDER_*` 没有单独配置，默认拿 coder 的端点与 key（意图分类与
问答用快模型即可），需要单独指定时可以用 `build_workflow` 的参数注入。
`config.py` 里对应 `xxx_configured()`，CLI 启动时一次性检查三组是否都配好。

---

## 6. 韧性设计

### 6.1 重试只在一层做

重试统一由节点层 `resilience.retry_call` 负责，SDK 层 `max_retries=0`。

原因是一次实测：reviewer 的模型端点挂起时，SDK 重试 3 次 × 节点重试 3 次
× 60s 超时 ≈ **9 分钟**才报错。两层重试相乘是典型的隐形成本放大，
且无法从代码上一眼看出。

同时，超时类错误默认**不重试**（`is_timeout_error`）：推理模型的超时通常意味着
"它本来就很慢"。实测 glm-5.3-flash 对一段 1.5KB 的代码审查提示要跑 **121 秒**
（7130 个 reasoning token），把这种超时重试三次只会把失败推迟到几分钟后。

两条合起来，最坏情况收敛到"一次请求超时"，而不是重试次数的乘积。
因此 `LLM_REQUEST_TIMEOUT` 要按模型的真实速度来设（推理模型建议 180s），
而不是按"网络请求应该很快"的直觉设。

```python
def retry_call(fn, *, attempts=3, base_delay=0.6, max_delay=5.0, sleeper=time.sleep):
    # 指数退避 min(base * 2^(n-1), max_delay)，始终返回 Attempt 而不抛异常
```

`sleeper` 可注入，测试里用 `lambda _: None` 秒过，不需要真的等。

### 6.2 降级矩阵

| 层 | 错误 | 处理 | fatal |
|---|---|---|---|
| 启动 | 某组 API Key 未配 | CLI 列出缺失项，退出码 1 | - |
| 节点 | 模型调用 5xx / 网络抖动 | 节点级指数退避重试 3 次 | - |
| 节点 | 模型调用超时 | 不重试，直接记错误终止（推理模型慢 ≠ 抖动） | ✅ |
| 节点 | 重试后仍失败 | 记错误 + 终止流程 | ✅ |
| 节点 | 输入为空（需求/代码） | 记错误 + 终止流程 | ✅ |
| 工具 | AST 分析抛异常 | 上下文替换为"不可用"，审查继续 | ❌ |
| 工具 | 执行环境不可用 | 返回结构化错误，fixer 不做无意义重试 | ❌ |
| 工具 | 策略拒绝执行 | 返回"已拒绝执行"文本，交给模型判断 | ❌ |
| fixer | 自验未通过 | 带错误反馈再修一次（可配置） | ❌ |
| 解析 | 模型输出 JSON 无法解析 | 包成一条 warning finding | ❌ |
| 图 | 死循环 | `RECURSION_LIMIT` 兜底，标记 aborted | ✅ |
| 记录 | 运行记录目录不可写 | 自动降级为不落盘 | ❌ |
| CLI | GraphRecursionError / Ctrl+C | 捕获提示，退出码 130 / 1 | - |

### 6.3 fixer 自验闭环

原实现里 fixer 只靠 prompt"改完就交"，本轮补上结构化自验：

```text
修复 → execute_code（策略校验 + 受限执行）
     ├─ 通过 → 交给 reviewer 复审
     └─ 不通过 → 把错误信息回灌给模型再修一次 → 再自验 → 交给 reviewer
```

**关键区分**：自验失败要分两类，只有"代码本身有问题"才值得再叫一次模型：

- 代码报错 / 被策略拒绝 → 可重试
- 临时目录不可写、子进程起不来 → `env_failed`，重试无意义，直接降级

自验结果写进 `self_check` 状态，reviewer 复审时能看到"上一轮自验过没过"，
不需要重新猜。

---

### 6.4 两个“跑真模型才暴露出来”的问题

这两个都不是靠想能想出来的，是一次真实调用之后从运行记录里查出来的：

**① 策略校验误伤了标准写法**

原策略是“任何双下划线名字/属性都拦”，结果 len('if __name__ == "__main__":') 也被拦了：
而 coder 提示词恰好鼓励写示例调用，于是“运行验证”几乎每轮都直接降级成
“被策略拒绝”，验证环节形同虚设。

修法是只放行无害的 `__name__` / `__doc__`，真正的逃逸向量
（`__class__` / `__globals__` / `__subclasses__` / `__builtins__`）继续拦。
判定原则：**安全策略的误报比漏报更难发现，因为它让“验证通过”变成一句空话。**

**② 模型会把控制标记漏进代码**

实测 fixer 输出末尾多了一行 `</parameter>`，直接导致 SyntaxError 第 98 行。
`strip_model_artifacts` 只处理**首尾**的纯标签行，中间的标签可能是字符串
字面量里的真实内容，不动它。

另外一个顺序细节：必须先去掉尾部的控制标记再去代码块，否则收尾的 ```
不在文本末尾，代码块正则匹配不上。

同时把 `exec_policy_reason` 写进消息 payload——否则记录里只能看到
“被策略拒绝”三个字，排查时得重跑一遍才知道为什么。


## 7. 运行记录与中断续跑

### 7.1 记录结构

`multi_agent/recorder.py` 每次运行写一份 `runs/<run_id>.json`：

```jsonc
{
  "schema_version": 1,
  "run_id": "16617fab",
  "created_at": "...", "updated_at": "...",
  "requirement": "...", "max_iterations": 2,
  "entry": "coder", "resumed_from": null,
  "last_node": "reviewer",        // 续跑推算的依据
  "timeline": [ {node, at, status, log[], messages[]} ],
  "snapshot": { /* 最近一次完整状态 */ },
  "result": { terminal, node_calls, elapsed_seconds, iteration, findings, errors }
}
```

写入方式是"临时文件 + `os.replace` 原子替换"，中途崩溃不会留下半截 JSON；
目录不可写时自动降级并给出提示，不阻断主流程。

### 7.2 为什么用执行器 + 自研记录，而不是 LangGraph checkpointer

`MemorySaver` 只活在进程内，跨进程续跑没有意义；`SqliteSaver` 需要额外依赖。
本项目需要的是"可读、可展示、可交付"的执行轨迹，自研 JSON 更贴合：
既是续跑依据，也是作业演示材料，还不引入新依赖。

### 7.3 续跑推算

`edges.next_node_after(last_node, state)` 把"上一个完成的节点 + 当前状态"
映射到下一步：

| last_node | 推算结果 |
|---|---|
| 无 | `coder` |
| `coder` | `reviewer` |
| `reviewer` | 路由为 fix → `fixer`，否则已结束 |
| `fixer` | `reviewer` |

`/resume` 用这个结果作为 `build_workflow(entry=...)` 的入口，
用记录里的 `snapshot` 作为初始状态，并新起一份记录（`resumed_from` 指向原记录），
不覆盖原文件。续跑以节点为粒度：会重跑入口节点，不做节点内断点。

---

## 8. 可扩展性设计

### 8.1 新增一个 Agent 节点

1. 在 `prompts/` 加一个新的 SystemPrompt
2. 在 `nodes/` 加一个 `make_xxx_node` + `run_xxx`（读状态、调模型、写状态）
3. 在 `graph.py` 里 `add_node` + `add_edge` 连进去

不需要改现有节点；新增状态字段一律可选，旧节点不感知。
例如"安全审查"节点：`reviewer → security → fixer`，只需一条边。

### 8.2 换模型

改 `.env` 里对应的 `*_LLM_BASE_URL` / `*_LLM_MODEL`，不需要改代码。
角色与模型解耦是本项目刻意的设计。

### 8.3 加新工具

在 `tools/` 写新工具，建议同时提供：

- `xxx_code(...) -> dict`：结构化结果，给节点内部消费
- `@tool xxx(...) -> str`：文本结果，给模型消费

然后在需要的节点里调用。工具层异常必须自己吞掉并返回结构化失败，
这是本项目对工具的一致约定。

### 8.4 换掉 CLI / 接入别的前端

`runner.run_workflow(app, state, on_event=...)` 与渲染完全解耦，
`on_event` 回调就能接出别的界面（Web、日志系统、测试断言）。

---

## 9. 测试策略

### 9.1 依赖注入

- `build_workflow(coder_llm=..., reviewer_llm=..., fixer_llm=...)` 三个模型可注入
- 节点纯函数 `run_coder / run_reviewer / run_fixer` 可直接调用单测
- `retry_call(..., sleeper=...)` 可注入，重试测试不需要真的等待
- 工具用 `unittest.mock.patch` 替换，制造确定性的降级/失败场景

### 9.2 测试分层（181 个用例，全部离线）

| 测试文件 | 覆盖内容 |
|---|---|
| `test_edges.py` | 入口分支 + 审后全分支（含 info 不阻断）+ 续跑推算 |
| `test_graph.py` | 代码任务四种结局 + 自由问答分支 + 消息时间线 + 版本历史 + 注入不串味 |
| `test_nodes.py` | router 意图识别（含显式 mode）、responder 回答、工具降级、自验与自修复 |
| `test_resilience.py` | 重试次数/退避上限、超时不重试、工具异常、LLM 封装、错误标记 |
| `test_runner.py` | 事件采集、JSON 落盘、时间线、续跑、结论判定（含 answered） |
| `test_severity.py` / `test_intent.py` | 阻断阈值、级别归一化、意图规则与模型兜底 |
| `test_conversation.py` | 多轮记忆、线程隔离、历史裁剪、工具回路、任务提案解析与结果回灌 |
| `test_file_writer.py` | 写文件边界、敏感文件拦截、确认后写入与覆盖 |
| `test_messages.py` / `test_text_utils.py` / `test_llm_factory.py` / `test_sandbox_policy.py` | 消息、文本兜底解析、单层重试、策略白名单 |

### 9.3 离线可复现

`examples/offline_demo.py` 用脚本化模型演两条分支（代码任务 + 自由提问），
打印完整消息时间线；`examples/check_env.py` 在开跑前查 DNS / 连通性 / 模型名。
`offline_demo.py` 不需要 API Key；`check_env.py` 需要已配置的 Key 与网络，
但只查询模型清单，不发起生成请求。

---

## 10. 评分维度对照

| 维度 | 权重 | 本方案落点 |
|---|---|---|
| **流程完整性** | 40% | 入口路由 + 编码 → 审查 →（修复 → 复审）闭环 → 通过/终止；代码任务产出代码，提问直接产出回答；两者都有消息时间线与运行记录；离线演示可复现 |
| **Agent 协作** | 30% | 五个流水线角色分工明确，三组独立模型配置；router / responder 复用 coder，chat 默认复用 fixer；共享状态黑板 + 结构化消息总线；两级条件边路由；致命错误保护；自验反馈回灌 |
| **可扩展性** | 20% | 加 Agent 三步走；节点工厂注入；图可指定入口；执行器/渲染解耦；换模型只改 .env |
| **文档** | 10% | README + 本文 + 自动导出 Mermaid + 状态/消息/错误三张表 + 韧性矩阵 |

---

## 11. 已知限制

1. **reviewer 结论依赖模型遵守 JSON 格式**：多级兜底解析后仍失败则降级为 warning finding。
2. **fixer 自验只覆盖"能跑通"**：执行成功 ≠ 业务语义正确，仍需 reviewer 复审。
3. **只处理单文件**：不支持多文件 / 目录级协作。
4. **续跑粒度是节点**：会重跑入口节点，不做节点内断点。
5. **记录不含 token 用量**：模型侧未回传统计时无法统计成本。
6. **三个模型都走 OpenAI 兼容协议**：如果某个厂商不走兼容协议，需要自定义 Client。
7. **没有并行分支**：当前是单链 + 循环；要并行审查需要 fan-out/fan-in，
   LangGraph 支持，但会引入结果合并策略，本 MVP 未做。
8. **对话层不做跨进程记忆**：`MemorySaver` 只在进程内，关掉即失效（按需求如此）。
9. **写文件是阻塞确认**：写入时会等你按 y/n，没有后台并行写。
10. **阻断阈值是全局的**：目前一套阈值适用于所有任务，不支持按任务粒度调优。
