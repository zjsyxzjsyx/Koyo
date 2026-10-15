# 知秋

知秋是本地 CLI 学习 Agent。运行 `zhiqiu` 先显示学习状态，再进入自由对话；需要时由知秋建议、用户确认后进入学习、自测或复习。用户指定一个知识点和 `.md`／`.txt` 资料、网页或 Notion 页面，学完后回答费曼问题；程序保存原题、原答、分维度评阅和复习任务，并按 FSRS 安排复习。模型负责教学、出题与语义评阅；业务写入、状态转换、总分计算和排期由程序执行。

## 功能概览

- **大模型接入**：OpenAI 兼容的 Chat Completions 适配器，每次调用记录耗时、token 用量与状态；按模型配置窗口、输出预留与余量。
- **上下文构建**：教学、澄清、出题、评阅分别使用版本化的固定模板；每次请求区分必需内容与可选内容，按预算取舍，并记录纳入、排除的来源及原因。连续澄清和出题在同一会话内追加消息，复用已测量的请求前缀。
- **会话与记忆**：会话跨进程恢复；语义记忆只接受用户显式设置；评阅成功后异步生成有来源的情景摘要；跨天追问旧讲解时先用摘要，来源被更正后摘要失效并按需重建。
- **评分**：同一维度可有多道题，题目难度在作答前固定；模型对每个维度给一个 0–4 分并引用原答逐字证据，程序核对题目覆盖与引文后按 50/30/20 计算总分。证据放不进一次调用时按维度、按题分批；仍无法可靠评阅时进入待评阅，不产生分数或排期。
- **评分质量检查**：人工标注模板与一致率核对工具，用于验证模型评分与人工判断的一致性。
- **对话与运行时**（V4）：默认自由对话（有界 ReAct 循环，可用全部工具）；模型只能“提议”进入流程或记住偏好，由用户确认、程序执行。业务状态和对话模式都有显式的状态转换表，每次迁移写事件，中断后读状态字段恢复。
- **观测与治理**（V5）：每轮对话或每次命令一条 trace（span 树，开始时即落库）；`zhiqiu dashboard` 本地网页看板实时显示运行到哪一步，并有 trace 瀑布图、指标与花费、学习记录；`metrics` 汇总成功率、延迟、token、花费与人工确认；护栏每次触发写 `GuardrailTriggered`，密钥按值集中脱敏；`check` 对真实记录做运行契约检查，`failures` 分类失败并可导出为回归样例；`gate` 是发布门禁。见“观测与治理”。

## 项目结构与职责

```text
.
├─ src/zhiqiu/
│  ├─ cli.py          CLI 命令、终端输入输出与用户确认
│  ├─ agent.py        `zhiqiu chat` 对话外壳：有界 ReAct 循环、提议与确认、斜杠命令
│  ├─ conversations.py 对话与对话消息的保存、按天划分、模式切换与删除
│  ├─ flows.py        讲解、自测与复习的输入循环（终端与对话模式共用）
│  ├─ flow.py         状态转换表与统一的状态转换入口
│  ├─ runtime.py      学习／复习流程编排与模型输出校验
│  ├─ context.py      分阶段上下文组装、预算检查与审计
│  ├─ model.py        OpenAI 兼容 Chat Completions 适配器
│  ├─ scoring.py      评分标准、证据校验、加权总分与 FSRS 档位映射
│  ├─ store.py        SQLite 权威状态、迁移、事务、审计事件与 FSRS 排期
│  ├─ notion.py       Notion 官方 API 读取页面并转成 Markdown
│  ├─ mcp_client.py   连接本地 stdio MCP 服务，白名单工具接入工具层
│  ├─ mcp_server.py   `zhiqiu mcp`：只读 MCP 服务（stdio），审计写 JSONL
│  ├─ queries.py      状态、到期、知识点与 trace 的只读查询（Store 与只读工具共用同一份 SQL）
│  ├─ memory.py       显式语义记忆、情景摘要与旧讲解摘要
│  ├─ retrieval.py    旧讲解追问的可解释排序
│  ├─ history.py      对话工具 search_history：讲解与对话的 FTS5 关键词检索
│  ├─ materials.py    本地资料读取、范围限制和内容哈希
│  ├─ search.py       Exa Search 适配器与 web_search 工具声明
│  ├─ tools.py        工具声明、注册、参数校验、执行闸门、重试、截断与审计
│  ├─ toolloop.py     资料准备与学习阶段澄清内的有界工具循环、搜索查询泄露检查
│  ├─ files.py        read_file（只读已授权资料）与 write_file（只写指定目录、逐次确认）
│  ├─ quality.py      人工标注模板、评分质量检查、评分提示指纹与评测记录
│  ├─ tracing.py      trace：span 树、当前 span（contextvars）、进度与中断标记
│  ├─ traces.py       trace 列表、树、实时运行链与终端时间线
│  ├─ metrics.py      指标聚合（读取时计算）；pricing.py 按 token 推算花费
│  ├─ guardrails.py   护栏触发的统一事件；redact.py 按值脱敏
│  ├─ contracts.py    运行契约检查（PASS／FAIL／证据不足）与失败分类
│  ├─ regressions.py  校验失败的模型原文导出为回归样例
│  ├─ gate.py         发布门禁
│  ├─ dashboard.py    `zhiqiu dashboard` 本地只读看板（dashboard.html 为页面）
│  ├─ diagnostics.py  模型联通、预算与窗口容量检查
│  ├─ local_env.py    读取项目根目录的 .env
│  └─ clock.py        系统时钟与测试用固定时钟
├─ tests/             存储、流程、上下文、评分、检索与诊断测试；test_scenarios.py 为端到端场景套件，
│                     regressions/ 为从真实失败沉淀的回归样例
├─ examples/          无密钥演示、真实服务联通与评分验收脚本
└─ labels/            人工标注与检索校准用的公开合成样本；eval-runs.jsonl 为评分评测记录（随代码提交）
```

设计原则是“模型提出建议，程序做出决定”。模型的输出一律视为不可信输入：`runtime.py` 校验结构，`scoring.py` 核对原题、原答与逐字证据；只有 `store.py` 改变业务状态，并在一个 SQLite 事务内保存评阅、FSRS 状态、复习任务与审计事件，提交后回读确认，CLI 才报告保存成功。

```mermaid
flowchart LR
    U[用户] --> CLI[CLI cli.py]
    CLI --> RT[流程编排 runtime.py]
    CLI -->|状态查询、修订、导出、删除| ST[状态存储 store.py]
    RT --> CTX[上下文 context.py]
    CTX --> MEM[记忆 memory.py]
    CTX --> ST
    RT --> MOD[模型适配 model.py]
    RT --> MAT[本地资料 materials.py]
    RT --> TOOLS[工具层 tools.py]
    TOOLS --> WEB[搜索适配 search.py]
    RT --> SCORE[评分校验 scoring.py]
    RT --> ST
    MOD --> LLM[OpenAI 兼容模型服务]
    WEB --> EXA[Exa Search]
    ST --> FSRS[Py-FSRS]
    ST --> DB[(SQLite：业务表、会话、记忆、审计)]
```

所有模型调用都经过 `LearningRuntime._invoke`：先由 `ContextManager.build` 按阶段组装请求并检查预算，再交给适配器发送，返回的必须是 JSON 对象。必需内容超预算时改用已检查过的资料摘要；服务端报告窗口溢出时去掉可选内容重试一次。业务流程中的调用次数与顺序由程序决定；只有对话模式和两个学习步骤开放有界工具循环（见“工具层”“对话模式”）。

## 学习与复习流程

`status` 和每次启动的到期提醒只查询任务；看到提醒不会完成复习。首次自测没有可评分回答时保留待补状态，不产生首次任务。

```mermaid
flowchart TD
    A[启动 CLI：只读展示到期任务] --> B{选择命令}
    B -->|learn| C[读取本地资料，列出文件、要获取的网页与 Notion 页面、是否联网搜索与模型端点并请求确认]
    C -->|确认后获取指定网页与 Notion 页面| D[模型判断资料是否充分]
    D -->|不足且允许 Web| E[有界工具循环补充资料，再判断资料]
    D -->|充分或用户选择本地来源| F[模型教学并展示来源]
    E -->|充分| F
    E -->|仍不足或搜索失败| X[停止学习，不创建自测任务]
    D -->|不足且离线| X
    F -->|/done| G[创建首次自测，保存题目和原答]
    B -->|resume| G
    G -->|/pause 或异常退出| H[保留进度，稍后 resume]
    G -->|自然结束、/end 或达到上限| I{有可验证的回答证据？}
    I -->|无| J[首次自测待补，不排期]
    I -->|有| K[校验三维证据并由程序计算总分]
    K --> L{评阅范围}
    L -->|完整| M[FSRS 计算到期日]
    L -->|阶段性| N[次日复习，不更新 FSRS]
    M --> O[同一事务保存评阅、状态和任务；提交后回读]
    N --> O
    O --> P[到期后 status 展示任务]
    B -->|review| Q[合并同知识点到期任务，开始复习问答]
    P --> Q
    Q --> R{复习结果}
    R -->|有评分| K
    R -->|零回答| S[原任务未完成，次日再提醒]
    R -->|连续困难达到阈值| T[标记需补学；完整自测开启新 FSRS 周期]
```

### 状态机

业务状态只能经 `flow.py` 的 `transition`／`enter` 改变：先按声明的转换表检查，合法才更新字段，并在同一事务内写一条 `StateChanged` 事件（实体、ID、原状态、新状态、原因）；表外的转换抛出 `IllegalTransition`（`ValueError` 的子类），字段和事件都不变。声明为“到自身”的转换（如进行中的尝试再次 `resume`）不改字段、不写事件。原有的业务事件（`AttemptPaused`、`ReviewSaved` 等）照常写入。

| 实体（字段） | 转换 |
|---|---|
| 尝试（`attempts.state`） | 新建→`in_progress`；`in_progress`→`paused`／`awaiting_review`／`completed`／`needs_self_test`；`paused`、`needs_self_test`→`in_progress`；`awaiting_review`→`completed`；`completed` 为终点 |
| 复习任务（`review_tasks.state`） | 新建→`pending`；`pending`→`started`／`relearn_handled`；`started`→`done`／`pending`（零回答推迟）／`relearn_handled`；`relearn_handled`→`pending`（修订撤销补学）；`done` 为终点 |
| 知识点（`knowledge_points.state`） | 新建→`active`；`active`↔`needs_relearn` |
| 讲解（`sessions.lesson_state`） | 新建→`teaching`；`teaching`→`tested`（同日开始自测）／`closed`（`/end` 或跨天续讲时关闭旧会话）；`tested`、`closed` 为终点 |
| 对话（`conversations.mode`） | 新建→`chat`；`chat`→`lesson`／`self_test`／`review`；`lesson`→`self_test`／`chat`；`self_test`、`review`→`chat` |

待继续的讲解直接读 `lesson_state='teaching'`，不再从消息推算。讲解关闭后，当天再开始的讲解进入新会话，可以正常继续。v14 迁移按已有消息回填 `lesson_state`（已关联尝试为 `tested`，有关闭记录为 `closed`，其余有讲解的会话为 `teaching`）。

运行边界：一次问答最多 8 题或 25 分钟。完整评阅按概念准确、机制解释、应用辨析分别给 0–4 分，权重 50/30/20，得分映射为 FSRS 的 Again、Hard、Good、Easy；阶段性评阅统一次日提醒，零回答不新建任务。`revise` 按修订后的分数重算受影响的 FSRS 状态和未开始任务的到期日。使用 Py-FSRS 6.3.2、90% 目标回忆率、365 天上限，关闭分钟级步骤和随机扰动。

## 对话模式

`zhiqiu chat`（或在交互终端中直接运行 `zhiqiu`）先显示完整状态，再进入对话；脚本和管道中直接运行 `zhiqiu` 仍只显示状态。每一轮：

1. 用户的话先写入 `conversation_messages`。
2. 用 `chat` 模板组装请求：固定规则、本段对话开始时冻结的偏好与人设记忆、学习状态快照、可读与可写目录是必需内容；较早对话的摘要和摘要之后的原文是可选内容，超出预算时先丢摘要之后最旧的原文，最后才丢摘要。
   - 学习状态快照只在对话启动和每次流程结束后刷新，两次刷新之间系统提示保持不变，请求前缀可以复用；对话中需要最新状态时由模型调用 `zhiqiu_status`。
   - 工具循环每次模型调用后记下服务端报告的 `prompt_tokens`（与业务流程的 `observe_usage` 同一机制），之后保留该前缀的请求按“已测量前缀 + 新增部分”估算预算。
   - 滚动摘要：未被摘要的历史估算超过输入上限的 70%（未配置预算时为超过 40 条）时，保留最近 10 条原文，把更早的消息连同已有摘要交给 `chat_summary` 阶段生成新摘要，写入 `chat_summaries`（记录覆盖到的消息序号与来源哈希），原文不删除；输入过长时只取较早的一半再试；生成失败时记入 `last_error`，本轮退回为丢弃最旧消息，照常回答。
3. 进入有界工具循环（沿用 V3：最多 5 轮、总时限 180 秒；用完时再要一次不调用工具的文字回答，最多等 30 秒）。回答写入对话记录，结束状态与轮数记在消息的 `payload_json` 中；拿不到回答时记入 `conversations.last_error`，对话继续。

对话中的工具规则：

| 工具 | 规则 |
|---|---|
| `read_file` | 只读 `ZHIQIU_READ_ROOTS` 白名单目录中的 `.md`/`.txt`，白名单外、`..`、符号链接一律拒绝；配置白名单即表示同意把其中的文件发送到模型端点，读取时不再逐次确认 |
| `write_file` | 同 V3：只写 `ZHIQIU_WRITE_ROOTS`，每次确认，覆盖前备份 |
| `web_search`、`fetch_url` | 调用前确认，显示查询或网址；同一轮对话中允许过一次后，该工具在本轮剩下的调用不再询问，但仍逐条显示（标注“本轮已允许”），拒绝不会被记住；参数不得包含本次运行中读到的笔记原文（连续 15 字）；`--offline` 时不提供 |
| 外部 MCP 工具 | 每次调用前确认，显示参数；其余同上 |
| `zhiqiu_*` | 只读业务查询，直接执行 |
| `search_history` | 按关键词搜索以前的讲解（含追问解答）与对话消息，返回知识点、日期和命中片段，直接执行；不含当前这段对话，只在对话中提供、不经 MCP 开放。见下方“历史检索” |
| `propose_learn`、`propose_review`、`propose_resume`、`propose_memory` | 权限类别 `propose`：只提出建议，调用后工具循环立即结束，同一轮中排在它之后的调用不执行 |

历史检索（`history.py`）用 SQLite 自带的 FTS5 全文索引，不用 embedding，也不调用模型：文本按旧讲解追问的同一规则切成中文二字词（去掉“什么”“为什么”等疑问词），以空格连接后建索引，用 BM25 排序，所以“旋转”这样的两字词也能命中。索引在每次检索前补上新增的讲解和对话、去掉已删除的；`forget` 与 `chat forget` 在同一事务里删掉对应索引项，并开启 FTS5 的 secure-delete。所用 SQLite 不含 FTS5 时不提供该工具。

提议由程序先按业务规则检查（资料须在可读目录内、复习须已到期且当日不超过 3 次、要继续的尝试或讲解须存在），不可执行时直接说明原因；可执行时展示并请用户确认，确认后才切换对话模式、进入 `flows.py` 中的流程，`learn` 仍会再确认一次发送哪些资料。同一提议用 `receipts` 中的 `proposal:<消息ID>` 防止重复执行。流程结束、暂停或中断后回到对话，并写一条 `flow_result` 摘要。

斜杠命令由程序直接执行，不经过模型：`/status`、`/learn 知识点 [--from 路径] [--url 网址] [--notion 页面]`、`/review [知识点]`、`/resume [ID]`、`/memory`、`/new`、`/help`、`/exit`。用户自己输入的命令不需要再确认是否进入流程。

中断与恢复：模型调用或工具执行中按 Ctrl+C 只取消当前一轮（写一条“已取消”）；在输入提示处按 Ctrl+C 或 EOF 退出对话，对话仍保持打开；确认提示中按 Ctrl+C 视为拒绝。启动时若对话模式停在流程中（例如上次崩溃），以业务表为准改回 `chat`（事件原因 `reconcile`），并提示可以 `/resume` 的自测或讲解，不会自动进入。

对话记录：

- 按天划分：当天再次进入接着当天的对话，第二天自动开新对话；`/new` 随时开新对话，`zhiqiu chat --resume ID` 回到旧对话。
- 永久保留，不自动清理。`zhiqiu chat list` 列出、`zhiqiu chat log ID` 查看（消息、摘要、模式切换事件、工具调用），`zhiqiu chat forget ID|--all` 删除；删除时一并删除该对话的摘要；工具审计保留，但不再关联到对话。`export` 包含对话与摘要表。
- 偏好与人设：模型可用 `propose_memory` 提议记住用户明确表达的长期偏好（key 以 `pref.` 或 `soul.` 开头，单条不超过 300 字，这两类合计不超过 2000 字）；用户确认后才写入语义记忆，来源记为 `chat:<消息ID>`。每段对话开始时冻结一份全局记忆快照，本段对话中新记住的内容从下一段对话开始生效；讲解、澄清照常使用全局记忆，评分不使用。

## 评分

评分标准写在 `scoring.py` 的 `RUBRIC`：0 为不会或完全错误，1 为有零散正确点但关键结论错误，2 为基础正确但深层不足，3 为基本完成但有一处明显不足，4 为完全答对或只有非关键遗漏，不因题目难度封顶。题目难度分为 `basic`、`advanced`、`transfer`，在作答前固定，作答后不能修改。同一维度有未答题时，未答题覆盖的要求视为未证明：只答了基础题且答对时最高 2，已答题中有答对的进阶或迁移题时最高 3。回答含糊时先在同一维度追问一次，再按澄清后的理解评分。

程序拒绝以下评分并让模型重评，校验失败的原因会反馈给重试：同一维度出现多项、漏评或重评某道题、引文不在原答中、未答题带有证据、分数不在 0–4。重试仍未通过则进入 `awaiting_review`。

原答本身超预算时，尝试进入 `awaiting_review`，原答保留。可用 `submit-final` 另存一份精简的评分答案，再用 `resume` 继续。评原答的分段路径（`resume ATTEMPT_ID --grade-original`）把原答按段落、句子边界切成无重叠且覆盖全文的片段，逐段提取正确点、错误点、未决点与短引文，进度按尝试、答案、内容哈希、规则版本和模型版本缓存，汇总时检查跨片矛盾。该路径默认关闭，需设置 `ZHIQIU_LONG_ANSWER_SEGMENTS=1`。

## 工具层

工具调用由程序决定在哪一步发生，模型最多提出参数。每个工具用 `ToolSpec` 声明名称、参数（JSON Schema 子集，必须 `additionalProperties=false`）、权限类别（`read`、`network`、`write_file`、`external_mcp`）、超时、重试次数和输出上限；同名工具重复注册直接报错，并写明两边的来源。模型看到的函数声明由同一份声明生成。

所有调用经过 `ToolExecutor.run`，顺序为：查找 → 参数校验（只校验、不纠正，失败不执行）→ 闸门（本步骤未允许该权限类别时拒绝；需要确认的工具在缺少确认回调、回调出错或用户拒绝时一律拒绝）→ 执行 → 截断输出 → 审计。失败用固定的错误类型表示：`invalid_args`、`denied`、`timeout`、`network`、`rate_limited`、`upstream`、`not_found`、`internal`；只有 `network`、`timeout`、`rate_limited` 按指数退避重试。网络请求流式读取响应，每读一块检查一次总时限，超时即中断。

每次调用写一行 `tool_calls`：工具、来源、权限、关联的知识点或尝试、参数摘要（字符串截到 200 字，名称含 key、token、secret 等的字段不记录）、是否经确认、状态、错误类型、尝试次数、耗时、结果数和输出字数。`trace` 会列出该尝试及其知识点准备阶段的工具调用；`export` 与 `forget` 同样覆盖这张表。V2 的 `search_calls` 只保留为历史，不再写入。

目前注册的工具有 `notion_page`（只供程序调用，见“Notion 页面资料”）、业务只读查询 `zhiqiu_status`、`zhiqiu_due`、`zhiqiu_knowledge_point`（资料只列清单、不含全文）、`zhiqiu_trace`（输出上限 20,000 字），以及 `read_file`、`write_file`（见下）、`fetch_url`（Exa contents 接口，只接受 http/https，网络类错误重试 1 次）和 `web_search`（Exa，30 秒总时限，网络类错误重试 1 次；为节省 token，每条结果最多 1500 字，每次调用合计最多 6000 字）。

业务流程中开放工具循环（受限 ReAct）的步骤只有两个：资料准备和学习阶段的澄清（`learn` 或讲解恢复时的提问）；对话模式另有自己的工具规则（见“对话模式”）。资料准备时，本地资料不足时，模型在最多 5 轮内反复调用本步骤允许的读取类与网络类工具补充资料，每轮的工具结果作为 tool 消息交回模型。模型不再调用工具、达到轮数上限、下一轮请求将超出输入预算，或超过整个循环的总时限（默认 180 秒）时循环结束；之后仍由程序重新判断资料是否充分，不充分则停止学习。工具结果计入上下文预算，`tool_calls` 记录每次调用所属的循环 ID 和轮次。出题、评分、保存和排期不在循环中；自测和复习中的澄清不开放工具。

学习阶段澄清可用 `read_file` 和 `write_file`。轮数用完或超过总时限时，程序再请求一次不允许调用工具的最终回答（这次请求最多等 30 秒）；仍拿不到有效回答时，改用不带工具的普通澄清。

总时限的检查点：每轮开始前、每次工具调用前都会检查；每次模型调用最多只等剩余时间（且不超过 60 秒）；超时后同一轮中尚未执行的工具调用不再执行，只回给模型一条“未执行”的结果。因此一次循环最多比总时限多出一次工具调用的时间。

- `read_file` 只能读取本次已授权的本地资料文件，不读符号链接，沿用本地资料的格式与大小限制。
- `write_file` 只能写 `ZHIQIU_WRITE_ROOTS` 列出的目录（用系统路径分隔符分隔多个目录，Windows 为 `;`；未配置时不注册该工具），只写 `.md`/`.txt`，路径中不能有符号链接；相对路径写入第一个目录，可新建子目录；覆盖前把原文件备份为同名 `.bak`；从不删除。每次写入都要用户确认，确认时显示完整路径、新建还是覆盖、字数和内容预览；无法确认的环境一律拒绝。

搜索查询可由模型自拟，但不得包含本地资料中连续 15 个字以上的原文（知识点名称本身不算；本地资料中的网址视为公开引用，不参与比较），长度不超过 200 字；不合规的查询记为 `invalid_args`，原因交回模型修改。不支持工具调用的脚本化模型只提出一次查询，被拒时改用知识点名称。

## Notion 页面资料

`learn --notion 页面链接或 ID` 通过 Notion 官方 API 读取页面，作为用户指定的资料：学习开始的统一确认会列出它，它计入发送授权，页面内容变化后需要重新 `learn`。一次设置：

1. 打开 Notion 的集成管理页面 <https://www.notion.so/profile/integrations>，新建一个内部（Internal）集成，权限只勾选读取内容（Read content），复制它的令牌（Internal Integration Secret）。
2. 把令牌写进 `.env`：`ZHIQIU_NOTION_TOKEN=...`。
3. 在要学习的页面（或它的父页面）右上角 `•••` → Connections（连接）里添加这个集成。没有连接的页面读不到，这也是权限的边界。

读取规则：

- 页面标题和全部子块按顺序转成 Markdown（标题、段落、列表、待办、引用、提示框、代码、公式、分割线、表格、书签等）；分栏和同步块只取其中内容；子页面只列标题、不展开；不认识的块类型标为“未支持的 Notion 块”，不会悄悄丢掉。
- 每页最多 30,000 字，超出部分截断并在来源说明中注明；最多 2,000 个块、嵌套 6 层；与本地、网页资料合计不超过 100,000 字。
- 页面不存在或未连接集成时提示去“连接”里添加；令牌无效时提示令牌问题；限流时自动重试 2 次。
- 工具名为 `notion_page`，只由程序在你指定页面时调用，任何工具循环都不向模型开放；Notion 内容视为私人资料，参与搜索查询的泄露检查。

## 只读 MCP 服务

`zhiqiu mcp` 以 stdio 方式把知秋的业务只读查询提供给其他 Agent（Claude Code、Hermes 等）：`zhiqiu_status`、`zhiqiu_due`、`zhiqiu_knowledge_point`、`zhiqiu_trace`。服务端只开放权限为 `read` 的工具，注册表中出现任何其他权限的工具就拒绝启动；不开放写入，也不开放联网或文件工具。

- 数据库以 SQLite 只读模式打开，每次调用单独打开、用完关闭；不建文件、不迁移。数据库不存在或版本不符时启动即失败，提示先用 CLI 打开一次。
- 每次调用写一行审计到数据库所在目录的 `mcp-audit.jsonl`（字段与 `tool_calls` 相同），不写数据库。
- 启动时不打印到期提醒，标准输出只留给 MCP 协议。
- 需要可选依赖 `mcp`（官方 Python SDK，MIT 许可，锁定在 `>=1.10,<2`，当前锁定 1.30.0）：`uv sync --extra mcp`。

接入方式（`--db` 可省略，默认用户数据目录下的 `zhiqiu/state.sqlite`；数据库须先被 4.0.0 版 CLI 打开过一次，完成 v15 迁移）：

- Claude Code（项目级配置，适合没有 `claude` 命令行的环境，例如桌面版）：在要使用知秋的项目目录放一个 `.mcp.json`。`D:\code\知秋\.mcp.json` 已按此写好：

  ```json
  {"mcpServers": {"zhiqiu": {"command": "uv",
    "args": ["--directory", "D:\\code\\知秋\\知秋V1.0", "run", "--extra", "mcp", "zhiqiu", "mcp"]}}}
  ```

  在该目录新开会话，按提示批准 `zhiqiu` 服务，然后问“知秋今天有哪些到期复习”，应调用 `zhiqiu_status`，并在数据库目录的 `mcp-audit.jsonl` 留下一行记录。要用非默认数据库时，在 `"zhiqiu"` 与 `"mcp"` 之间加入 `"--db", "数据库路径"`。
- Claude Code（有 `claude` 命令行时）：

  ```powershell
  claude mcp add zhiqiu -- uv --directory D:\code\知秋\知秋V1.0 run --extra mcp zhiqiu mcp
  ```
- Hermes（本版搁置，未做真实接入，仅供参考）：在 `config.yaml` 的 `mcp_servers` 段增加一项，`command` 为 `uv`，`args` 为 `["--directory", "D:\\code\\知秋\\知秋V1.0", "run", "--extra", "mcp", "zhiqiu", "--db", "D:\\path\\to\\state.sqlite", "mcp"]`。本机的 Hermes 学习源码不含 MCP 配置解析部分，字段名请以 Hermes 文档为准。

[server.json](server.json) 按官方 MCP Registry 的 2025-12-11 格式登记本服务（PyPI 包 `zhiqiu`，经 `uvx --from zhiqiu[mcp]==4.0.0` 以 stdio 启动），只用于本地记录和以后发布，目前没有发布到 Registry 或 PyPI。测试会用 `tests/data/server.schema.json`（官方 schema 的本地副本）离线校验它，并核对版本号与 `pyproject.toml` 一致。

## 外部 MCP 工具（客户端）

配置 `ZHIQIU_MCP_CONFIG` 指向一个 JSON 文件后，`learn` 会启动其中列出的本地 stdio MCP 服务，把白名单内的工具接入工具层，供资料准备的工具循环和对话模式使用（对话中每次调用前确认；学习阶段澄清、自测和复习不开放外部工具）。示例见 [examples/mcp.example.json](examples/mcp.example.json)（官方 `mcp-server-fetch`）：

```json
{"servers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"],
  "tools": {"fetch": {"permission": "external_mcp", "timeout_s": 60, "max_output_chars": 6000}}}}}
```

- 只注册 `tools` 白名单里且服务端确实提供的工具，名字为 `<服务名>__<工具名>`；与已有工具重名时报错并写明两边来源。权限只能是 `read`、`network` 或 `external_mcp`（默认）。
- 学习开始的统一确认会按服务列出可能调用的外部工具。外部工具的参数同样经过泄露检查，返回内容作为补充资料（`web`），不计入发送授权，由程序重新判断资料是否充分。
- 返回 `isError` 时记为 `upstream`；超过 `timeout_s` 时取消这次请求并记为 `timeout`；某个服务启动失败时跳过它，并在 `tool_calls` 记一条 `<服务名>__*` 的失败记录，其余服务照常使用。服务的标准错误输出写到数据库目录下的 `mcp-servers.log`。
- 需要可选依赖 `mcp`；离线模式（`--offline`）不启动外部服务。

## 记忆与跨天追问

- `memory set/list/delete` 管理语义记忆，需给出作用范围与来源；知识点专属的同名设置覆盖全局设置。对话中只有经用户确认的 `propose_memory` 会写入记忆，不会自动保存。
- 跨天追问旧讲解时开启新会话，通过来源 ID 关联旧讲解；摘要按需生成并缓存，`correct-lesson` 追加更正后旧摘要失效。
- `ask` 未指定 `--source` 且有多次旧讲解时，按追问内容对旧讲解排序：汉字二元组按候选集合做 IDF 加权重合，英文与数字按整词匹配，“为什么、怎么、而不是”等疑问与虚词短语不参与匹配，再加上最长共同片段和较弱的时间衰减。最高分达到阈值且明显领先时自动选用并说明，否则列出前 3 个候选，提示用 `--source` 指定或用 `--all` 逐次处理。每次排序的分数、信号和取舍理由记为 `LessonRetrieval` 事件。排序参数用 `labels/retrieval/cases.json` 校准（`examples/calibrate_retrieval.py`）。

## 观测与治理（V5）

回答四个问题：知秋做了什么、为什么这样做、结果是否可靠、出错后怎么定位。全部数据在本地 SQLite 中，不接 OTel、不发往外部服务。

### Trace

- 一轮对话、一次 `learn`／`resume`／`review`／`ask`／`resume-all` 命令各是一条 trace，是一棵 span 树：命令或对话轮 → 流程（学习、自测、复习、继续讲解）→ 工具循环、阶段（资料评估、讲解、出题、评分……）。模型调用（`llm_calls`）、工具调用（`tool_calls`）、业务事件（`events`）和上下文构建都挂在当时的 span 下（`trace_id`、`parent_span_id`）。
- span 在**开始时**就写入（状态 `running`），结束时补耗时与状态（`ok`、`error`、`interrupted`），运行中原地更新进度（第几题、工具循环第几轮、正在调用哪个工具），所以看板能实时显示运行到哪一步。进程崩溃留下的 `running` span，下次启动 CLI 时标为 `interrupted`。
- 模型调用另记开始时间、缓存命中 token（DeepSeek 的 `prompt_cache_hit_tokens` 或 OpenAI 的 `cached_tokens`）和错误类别；输出未通过校验时记 `error_kind=invalid_output`，不再显示为成功。
- trace 只记录元数据，trace 写入失败不影响学习流程。`forget` 删除知识点的 span；`chat forget` 保留 span 但解除与对话的关联；`export` 包含 `spans` 与 `llm_failures`。
- `zhiqiu traces` 列出最近的 trace；`zhiqiu trace ID --timeline` 按树形打印，ID 可以是 trace、尝试、对话或讲解会话；不带 `--timeline` 时输出与 V4 相同的尝试 JSON。

### 本地看板

`zhiqiu dashboard [--port 8765] [--open]` 启动只读网页看板。**聊天、学习时用的 `--db` 必须和看板相同**，否则看板看的是另一个数据库；看板启动时会打印它读取的文件，左下角也会显示。布局参考 waku（左侧导航带数量）与 Langfuse（trace 树加节点详情）：

- **实时**：每 1.5 秒刷新。显示正在运行的任务：模型调用、token、缓存命中率、花费；学习流程有“准备资料 → 讲解 → 自测 → 评分”的步骤条，当前步骤高亮并显示进度（如“等待回答 · 第 3 题”）；下方是当前的运行链路。
- **Trace**：最近的 trace 列表，可以按名称或知识点筛选、按状态过滤，每行带耗时条。点开后是带耗时条的调用树（可折叠，节点按类型用图标区分，出错的节点标红，状态迁移可以开关显示）；右侧是选中节点的详情：span 的耗时与 token 合计；模型调用的模板、纳入来源、预算，校验失败时的模型原文和导出回归样例的命令；工具调用的参数摘要与确认结果；护栏的规则与原因。
- **指标与花费**、**失败**、**契约检查**：分别对应 `zhiqiu metrics`、`zhiqiu failures`、`zhiqiu check`。
- **学习记录**：每次自测或复习的题目、回答、分维度评分与引文、复习排期、状态迁移，以及相关 trace。

模型调用的详情默认只有元数据。想查看每次调用发了什么、回了什么，在 `.env` 设置 `ZHIQIU_TRACE_CONTENT=1`：之后的调用会把请求消息与回复正文存进 `llm_contents`（脱敏，请求最多 6 万字、回复最多 2 万字；`forget`、`chat forget` 一并删除）。这会把学习资料原文也存进库里，所以默认关闭。

看板只绑定 `127.0.0.1`、只接受 GET、以只读方式打开数据库（终端里的知秋可以同时运行），所有输出经过脱敏；页面不加载任何外部资源。看板显示的内容与 `zhiqiu trace` 相同，**不要把端口转发或暴露到本机以外**。

### 指标与花费

`zhiqiu metrics [--days 7] [--json]` 在读取时汇总：按阶段的调用次数、错误率、无效输出率、p50／p95 延迟、输入／缓存命中／输出 token 与花费；工具按状态的分布与重试率；工具循环结束原因；命令、对话与流程的结果分布；人工确认次数与被拒率、提议结果、人工改分、评分延期；各护栏的触发次数。库里只存 token，金额按 `ZHIQIU_PRICE_*` 在读取时计算（价格会变）；只统计，不设上限。

### 护栏留痕与脱敏

运行时护栏沿用 V3／V4，V5 让每次触发都写一条 `GuardrailTriggered` 事件（`rule`、`action`、`reason`，挂在当前 trace 上）：

| rule | 触发 | action |
|---|---|---|
| `leak_check` | 联网参数夹带本地资料原文或过长 | blocked |
| `read_root`／`write_root` | 读写白名单以外的路径、符号链接、`..` | blocked |
| `name_not_allowed`／`permission_denied` | 本步骤不允许的工具或权限类别（含 `--offline` 下的联网工具） | blocked |
| `confirmation_denied` | 用户拒绝了确认（含拒绝发送学习资料） | denied_by_user |
| `confirmation_unavailable` | 需要确认但当前环境无法确认 | blocked |
| `proposal_rejected`／`proposal_refused` | 用户拒绝提议／提议未通过执行前校验 | denied_by_user／blocked |
| `memory_limit` | 记忆 key 前缀或字数超限 | blocked |
| `loop_limit` | 工具循环以轮数、时限或预算上限结束 | stopped |
| `output_invalid` | 模型输出未通过校验（出题、评分、无效 JSON） | rejected |
| `illegal_transition` | 非法状态转换（被拒的改动已回滚，事件单独保留） | blocked |
| `consent_missing` | 资料或模型端点未获发送授权 | blocked |

密钥按**值**集中脱敏（`redact.py`）：已配置的 `ZHIQIU_LLM_API_KEY`、Exa 与 Notion 令牌的实际值，以及 `sk-…`、`Bearer …`、`ntn_…`、`secret_…` 格式，在写入工具审计、对话消息与错误、`mcp-audit.jsonl`、`llm_failures` 和看板输出前替换为 `[redacted]`；原有的按参数名脱敏保留。

### Eval：场景套件、运行契约与失败沉淀

- **运行契约检查**（`zhiqiu check [--days 7] [--json]`）：对已记录的运行逐项给出 PASS／FAIL／证据不足（没有可检查的数据时说“证据不足”，不当作通过），有 FAIL 时退出码为 1。检查项：工具循环轮数与结束原因（`loop_rounds`）、工具调用轮次在循环之内（`tool_round_bound`）、提议之后同一轮不再执行工具（`proposal_stops_round`）、写文件与对话中联网都经过确认（`confirmed_side_effects`）、状态迁移按转换表重放且首尾衔接（`state_replay`）、模型与工具调用都挂在存在的 span 上（`trace_linked`）、没有遗留的运行中 span（`spans_closed`）。全部通过只说明程序守住了规则，不代表讲解或评分质量。
- **场景套件**（`tests/test_scenarios.py`）：用脚本化模型端到端运行学习→自测→评分、对话调用工具、提议→确认→流程→回到对话、自测中提问隔离、泄露拦截与确认被拒、中断与恢复；每个场景结束后契约检查必须没有 FAIL，trace 必须是一棵连通的树。`tests/test_contracts.py` 用人为构造的违规记录检验每个检查器确实会判 FAIL。
- **失败分类**（`zhiqiu failures [--days 7] [--json]`）：模型错误、上下文溢出、无效输出（按阶段，不允许的追问单列 `invalid_followup`）、工具超时与错误、各护栏、工具循环上限、评分延期、流程中断与出错，每类附最近的样例 trace。
- **失败沉淀**：输出未通过校验时，模型原文（脱敏、最多 2 万字）写入 `llm_failures`，`forget`／`chat forget` 一并删除。`zhiqiu failures export FAILURE_ID --to tests\regressions\NAME.json --expect rejected|recovered` 导出为回归样例，`tests/test_regressions.py` 把每个样例重放进同一套校验。第一条样例来自 V4 遗留的“不允许的追问”：V5 起出题校验失败会带着校验错误重试一次（`QUESTION_RETRIES=1`），重试给出合法题目即恢复。

### 发布门禁

`zhiqiu gate [--skip-tests] [--pytest-args "..."]`（只能在源码目录运行）：

1. 运行全量 pytest（含场景套件与回归样例），必须 100% 通过，不联网、不调用真实模型；
2. 计算评分提示指纹（出题、识别输入、各评分阶段的模板正文，共用的数据规则，`RUBRIC_VERSION` 与维度权重；`chat` 等其他模板的改动不影响指纹），要求 `labels/eval-runs.jsonl` 中有这个指纹的通过记录。

改了评分或出题提示却没有重跑人工标注，门禁就不通过，并提示运行 `examples/generate_label_samples.py --runs 1 --out <临时目录>`（约 30 次真实模型评分调用）。该脚本和 `zhiqiu evaluate --record` 会把结果连同指纹、提交、模型、一致率、token 与花费追加到 `labels/eval-runs.jsonl`。门禁本身从不调用真实模型。

## 安装与配置

需要 Python 3.12 和 `uv`。以下命令在包含 `pyproject.toml` 的项目目录运行：

```powershell
uv sync --python 3.12 --extra test
# 需要 MCP 服务或 MCP 客户端工具时：
uv sync --python 3.12 --extra test --extra mcp
uv run --python 3.12 python examples/demo.py
```

演示使用固定时钟和脚本化模型，不需要 API 密钥。

CLI 自动读取项目根目录的 `.env`（已被 Git 忽略）；已设置的进程环境变量优先。至少需要：

```powershell
$env:ZHIQIU_LLM_BASE_URL = 'https://your-provider.example/v1'
$env:ZHIQIU_LLM_MODEL = 'your-model'
$env:ZHIQIU_LLM_API_KEY = 'your-key'
```

可选配置（都可以写进 `.env`，`ZHIQIU_LONG_ANSWER_SEGMENTS` 除外）：

| 变量 | 作用 | 不配置时 |
|---|---|---|
| `ZHIQIU_EXA_API_KEY` | Exa 联网搜索补充资料、`learn --url` 获取网页正文 | 不能联网补充，不能用 `--url` |
| `ZHIQIU_NOTION_TOKEN` | `learn --notion` 读取 Notion 页面（Notion 内部集成令牌，设置见“Notion 页面资料”） | 不能用 `--notion` |
| `ZHIQIU_WRITE_ROOTS` | 学习中澄清和对话时允许写笔记的目录，多个用 `;` 分隔，例如 `D:\notes\zhiqiu` | 不注册 `write_file` |
| `ZHIQIU_READ_ROOTS` | 对话中允许直接读取的笔记目录（白名单），多个用 `;` 分隔；也是对话中 `propose_learn` 可指定资料的范围 | 对话中不能读文件 |
| `ZHIQIU_MCP_CONFIG` | 外部 MCP 工具配置文件，例如 `D:\code\知秋\知秋V1.0\examples\mcp.example.json` | 不启动外部 MCP 服务 |
| `ZHIQIU_MODEL_BUDGETS` | 按模型配置窗口、输出预留与余量（见下文） | 沿用下面三个变量 |
| `ZHIQIU_LLM_WINDOW`、`ZHIQIU_LLM_OUTPUT_RESERVE`、`ZHIQIU_LLM_MARGIN` | 未在 `ZHIQIU_MODEL_BUDGETS` 中匹配的模型的预算 | 不做超限判断 |
| `ZHIQIU_LLM_WINDOW_VERIFIED` | 设为 `1` 表示窗口已实测（只记入审计） | 视为未实测 |
| `ZHIQIU_LONG_ANSWER_SEGMENTS` | 设为 `1` 开启长答分段评阅（只能用进程环境变量） | 关闭 |
| `ZHIQIU_PRICE_INPUT`、`ZHIQIU_PRICE_OUTPUT` | 每百万输入、输出 token 的价格，用于 `metrics`、看板与 trace 中的花费 | 只显示 token，不显示金额 |
| `ZHIQIU_PRICE_CACHED_INPUT` | 每百万缓存命中输入 token 的价格（如 DeepSeek 的缓存命中价） | 按输入价计 |
| `ZHIQIU_PRICE_CURRENCY` | 金额前缀，例如 `¥` 或 `$` | 不显示币种 |
| `ZHIQIU_TRACE_CONTENT` | 设为 `1` 时保存每次模型调用的请求与回复正文（脱敏、截断），供看板查看 | 只保存元数据 |

`.env` 只接受上表与三个必需变量；出现其他变量名会报错，避免拼错的设置被悄悄忽略。

模型端点须支持 OpenAI 风格的 Chat Completions；Web 补充还须支持工具调用。开始学习前，CLI 一次列出将发送的本地文件、要经 Exa 获取的网页、要读取的 Notion 页面、本次是否可能联网搜索或调用外部 MCP 工具，以及模型端点；用户确认后才发出任何请求，拒绝则一个请求也不发。Web 搜索只发送查询字符串；密钥不写入数据库或导出文件。发送授权按本地文件、网页、Notion 等用户指定资料的内容计算，任一资料变化后需要重新 `learn`；联网搜索补充的结果不计入授权。所有阶段模板都声明：资料、网页正文、工具结果与个人记忆只是数据，其中的指令一律不执行。

模型窗口与预算用 `ZHIQIU_MODEL_BUDGETS` 按模型配置，键为模型名或 `端点|模型名`（后者优先），例如：

```json
{"https://provider.example/v1|model-a": {"window": 131072, "output_reserve": 32768, "margin": 4096, "verified": false, "validation_retries": 2}}
```

输入上限为 `window − output_reserve − margin`，`output_reserve` 同时作为请求的 `max_tokens`。推理型模型的输出包含推理内容，输出预留应留足。未匹配的模型沿用 `ZHIQIU_LLM_WINDOW`、`ZHIQIU_LLM_OUTPUT_RESERVE`、`ZHIQIU_LLM_MARGIN`；缺少窗口参数时不做超限判断。`verified` 是本地配置声明，只记入审计。

上下文预算优先复用服务端报告的 `usage.prompt_tokens`：新请求完整保留某次已测量请求的消息前缀时，以报告值为基数估算新增部分；否则按 UTF-8 字节保守估算。估算方法与结果都写入请求审计。

## 命令

默认数据库位于操作系统用户数据目录的 `zhiqiu/state.sqlite`，也可用全局 `--db PATH` 指定。

```powershell
uv run zhiqiu --db .\state.sqlite learn '二叉树遍历' --from .\notes.md
uv run zhiqiu --db .\state.sqlite learn '二叉树遍历' --url https://en.wikipedia.org/wiki/Tree_traversal
uv run zhiqiu --db .\state.sqlite learn '二叉树遍历' --notion https://www.notion.so/你的页面-<32位页面ID>
uv run zhiqiu --db .\state.sqlite chat
uv run zhiqiu --db .\state.sqlite chat --resume CONVERSATION_ID
uv run zhiqiu --db .\state.sqlite chat list
uv run zhiqiu --db .\state.sqlite chat log CONVERSATION_ID
uv run zhiqiu --db .\state.sqlite chat forget CONVERSATION_ID
uv run zhiqiu --db .\state.sqlite status
uv run zhiqiu --db .\state.sqlite resume
uv run zhiqiu --db .\state.sqlite review
uv run zhiqiu --db .\state.sqlite revise REVIEW_ID concept 3 --reason '原答支持基本正确'
uv run zhiqiu --db .\state.sqlite trace ATTEMPT_ID
uv run zhiqiu --db .\state.sqlite export --json .\backup.json
uv run zhiqiu --db .\state.sqlite forget '二叉树遍历'
uv run zhiqiu --db .\state.sqlite memory set '语言偏好' '使用 Python 示例' --source '用户明确设置'
uv run zhiqiu --db .\state.sqlite memory list --point '二叉树遍历'
uv run zhiqiu --db .\state.sqlite ask '二叉树遍历' '之前如何解释前序？'
uv run zhiqiu --db .\state.sqlite ask '二叉树遍历' '之前如何解释前序？' --all
uv run zhiqiu --db .\state.sqlite resume-all QUERY_ID
uv run zhiqiu --db .\state.sqlite correct-lesson MESSAGE_ID --text '原讲解中的更正'
uv run zhiqiu --db .\state.sqlite submit-final ANSWER_ID --text '用于评分的精简答案'
uv run zhiqiu --db .\state.sqlite resume ATTEMPT_ID --grade-original
uv run zhiqiu --db .\state.sqlite labels-template --json .\human-labels.json
uv run zhiqiu --db .\state.sqlite evaluate --labels .\human-labels.json
uv run zhiqiu --db .\state.sqlite evaluate --labels .\human-labels.json --record
uv run zhiqiu --db .\state.sqlite dashboard --open
uv run zhiqiu --db .\state.sqlite traces --days 1
uv run zhiqiu --db .\state.sqlite trace TRACE_OR_ATTEMPT_OR_CONVERSATION_ID --timeline
uv run zhiqiu --db .\state.sqlite metrics --days 7
uv run zhiqiu --db .\state.sqlite check --days 7
uv run zhiqiu --db .\state.sqlite failures --days 7
uv run zhiqiu --db .\state.sqlite failures export FAILURE_ID --to tests\regressions\some_case.json --expect rejected
uv run zhiqiu gate
```

会话内 `/done` 确认学完并开始自测，`/pause` 保存问答进度，`/end` 明确结案，`/sources web|local` 纠正资料选择，`/accept N` 接受关联知识点建议，`/question 文本` 记录并解释澄清问题；讲解阶段直接输入不以 `/` 开头的文字也按澄清问题处理（以 `/` 开头但不认识的输入仍提示“未知命令”）。直接关闭或异常退出不会自动结案。讲解、自测与复习的输入循环在 `flows.py` 中，CLI 命令与对话模式共用。自测和复习中，`/answer 文本` 直接作为回答保存，`/question 文本` 直接作为提问；不带前缀的输入先由模型判断是回答还是提问（`classify_turn`，拿不准时判为提问）：判为回答直接保存；判为提问时请用户确认（回车确认，输入 `a` 改为回答），确认的提问按澄清处理、记为不计入评分的笔记（事件标记 `via=classified`），评分请求看不到它；判断失败时请用户选择 `a` 或 `q`。不支持判断的脚本化模型按回答保存。`--offline` 禁止 Web 补充，也不能使用 `--url`、`--notion`。`--url`、`--notion` 可重复，可与 `--from` 同时使用，三者至少给出一个；每个网页最多取 15,000 字，超出部分截断并在来源说明中注明，本地、网页与 Notion 资料合计不超过 100,000 字。网页或 Notion 页面获取失败时学习停止，不保存任何资料。低分维度关联的原题原答会成为后续复习出题的重点。

## 验证与诊断

```powershell
uv run --python 3.12 --extra test python -m pytest -q
```

测试覆盖迁移与回滚、会话恢复、幂等任务、只读提醒、上下文预算与审计、记忆失效、多题评分与证据校验、待评阅恢复、长答分段、旧讲解排序、工具层（校验、闸门、重试、总时限、审计）、诊断逻辑，以及 V5 的 trace、指标、护栏事件、脱敏、运行契约、端到端场景、回归样例、门禁和看板接口；均使用脚本化模型，不需要密钥。发布前运行 `uv run zhiqiu gate`。

模型联通、预算与窗口容量用 `check-model` 检查，只发送公开合成文本，不输出密钥、请求正文或模型答复：

```powershell
uv run zhiqiu check-model              # 联通、模型身份与服务端 token 计数
uv run zhiqiu check-model --calibrate  # 按服务端计数扩大样本，检查所配输入上限
uv run zhiqiu check-model --boundary   # 发送接近输入上限的请求
uv run zhiqiu check-model --capacity   # 生成满额输出预留，并在接近输入上限时再生成一次
```

`--capacity` 在两次满额生成都达到输出预留的 98%、且第二次输入达到上限的 99% 时，标记 `window_size_verified`；随后发送超出窗口 3% 的请求（只要求 16 个输出 token），被拒绝说明配置与服务上限一致，被接受说明配置是保守下限。它会消耗接近整个窗口的 token。只验证检查流程时，可在命令进程中临时设置小窗口，不改 `.env`：

```powershell
$env:ZHIQIU_MODEL_BUDGETS = '{"https://provider.example/v1|model-a":{"window":16384,"output_reserve":4096,"margin":512,"verified":false}}'
uv run zhiqiu check-model --capacity
Remove-Item Env:ZHIQIU_MODEL_BUDGETS
```

真实服务检查只使用公开合成内容，不读取个人学习资料。`live_smoke.py` 让模型发出一次 `web_search` 工具调用，再经工具层调用 Exa，不打开数据库；`live_v2_smoke.py` 在临时数据库中跑完整学习流程；`live_v4_smoke.py` 在临时数据库中跑对话模式（状态查询、提议进入学习、自测中提问、记住偏好、较早对话的滚动摘要），只用公开合成笔记、不联网：

```powershell
uv run --python 3.12 python examples/live_smoke.py
uv run --python 3.12 python examples/live_v2_smoke.py
uv run --python 3.12 python examples/live_v2_smoke.py --all-model-questions
uv run --python 3.12 python examples/live_v4_smoke.py
uv run --python 3.12 python examples/live_v5_smoke.py
```

评分质量用人工标注检查：`labels-template` 导出原题、原答、模型分数和证据，人工在 `human_score` 填入 0–4 后运行 `evaluate`，核对精确一致率（至少 30 项、≥80%）并逐项检查引文仍在原答中。`labels/` 中的公开合成样本可用以下脚本评分与核对：

```powershell
uv run --python 3.12 python examples/generate_label_samples.py --runs 3
uv run --python 3.12 python examples/generate_label_samples.py --set multi --runs 3
uv run --python 3.12 python examples/long_answer_samples.py
```

`--runs N` 把同一批样本评分 N 次、写入各自的数据库，并按平均精确一致率判定。`--out DIR` 把数据库与生成文件写到别处，不覆盖本地已有的标注结果。每次真实评分都会追加一条记录到 `labels/eval-runs.jsonl`（含评分提示指纹），供 `zhiqiu gate` 核对。

`live_v5_smoke.py` 在临时数据库中用真实模型跑一轮对话（调用工具）和一次完整学习（讲解→自测→评分），然后核对 trace 树完整、`check` 没有 FAIL、指标里有 token；运行时可另开 `zhiqiu --db <脚本打印的路径> dashboard` 观察实时进度。

## 已知限制

- 同一输入在温度 0 下的评分仍可能相差一档，单次一致率会有波动，评分质量应按多次评分的平均值判断。
- 单道长答的分段评阅默认关闭：要点较多的长答在汇总步骤可能超出预算，需先实现分批汇总并通过人工样本检查后再启用。
- `verified` 只是配置声明，不会由程序自动推断；未做满额实测的窗口应保持 `false`。
