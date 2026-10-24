# 观测、评测与门禁

Trace、看板、指标与花费、护栏、Eval、发布门禁与诊断命令。返回 [README](../README.md)。

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
- **实时架构图**（9.1，参考 waku 的实时架构图）：实时页最上面是一张知秋的结构图，分“一轮对话”（入口 → 对话一轮 → 上下文组装 → 工具循环［模型 ⇄ 审批 ⇄ 工具］→ 回答）、“学习流程”（资料评估 → 讲解 → 出题 → 评阅 → 排程）、“状态库”（知识点与尝试、记忆、对话与摘要、复习日程）和“观测”（Trace、契约检查、指标与花费、护栏与失败）四块，每个框下面是今天的数字，点击跳到对应页面。图有两层亮法：
  - 正在运行的 span 所在的框保持高亮；链路最深处如果是工具循环，等模型时亮“模型”、调用工具时亮“工具”；有待批准的动作时，“审批”菱形保持橙色，直到批准、拒绝或取消。讲解、评阅这类要一两分钟的步骤因此一直看得出卡在哪。
  - 打开页面之后发生的事按顺序回放，每 0.6 秒一个：经过的框亮约 1 秒，箭头变成流动虚线，失败或被拒的闪红；图下面的状态行和“最近事件”列表（最新 12 条，可点到 trace）写着同一句话。积压超过 30 个只播最新的，并注明跳过了几个；系统开启“减少动态效果”时不播动画，只更新文字。
  - 数据来自数据库，所以终端里的 `zhiqiu chat`、`zhiqiu learn` 和网页工作台一样会点亮这张图，只读看板也能用。事件与框的对应关系写在 `dashboard.html` 的 `liveSpec` 里，和画图的 `archSVG` 放在一起。接口 `/api/live/events?after=游标&limit=200[&stats=1]`：把 span（对话一轮、流程的开始与结束，阶段与工具循环的开始）、上下文组装、模型调用、审批（提出与决定）、工具调用和业务事件合成一条按时间排序的事件流。模型和工具调用在结束时才写入，所以按结束时间计；游标比当前时间晚 2 秒，结束后稍晚落库的记录也不会漏掉。不带 `after` 时只返回游标，不回放历史；`stats=1` 附带各框的今日数字；`after` 不是整数时返回 400。
- **Trace**：最近的 trace 列表，可以按名称或知识点筛选、按状态过滤，每行带耗时条。点开后是带耗时条的调用树（可折叠，节点按类型用图标区分，出错的节点标红，状态迁移可以开关显示）；右侧是选中节点的详情：span 的耗时与 token 合计；模型调用的模板、纳入来源、预算，校验失败时的模型原文和导出回归样例的命令；工具调用的参数摘要与审批结果（策略放行、用户批准、本轮已允许、被拒或无需确认）；护栏的规则与原因。
- **指标与花费**、**失败**、**契约检查**：分别对应 `zhiqiu metrics`、`zhiqiu failures`、`zhiqiu check`。
- **审批**（V6）：每次询问用户的记录（见“审批策略”），显示发起时间、工具、描述首行、结果（待审批、已批准、已拒绝、已取消）、在终端还是网页决定、等了多久，可跳到对应 trace；导航上的数字是当前待审批数。接口 `/api/approvals?days=&status=`。在工作台里（7.1），本工作台正在等待的审批在这一页默认展开完整描述，并有“批准”“拒绝”按钮，和对话里的审批卡片是同一个提示，哪边先回答算哪边（另一边再回答得到 409），对话卡片会标出“已批准（审批页）”；终端发起的待审批只显示“在发起的界面回答”。
- **复习日程**（7.1）：周视图（周一到周日）和月视图（整月日历，补齐首尾的周），逾期的条目单独列在上方；每个条目显示知识点、上次得分、排期规则（FSRS 或次日补测），推迟过的显示“原定 MM-DD，推迟 N 次”，进行中的复习带标签；过去的日子显示当天完成的复习。下方是未来 14 天的负载柱状图（逾期单独一根），超过每日上限（`DAILY_REVIEW_LIMIT = 3`）的那天标红；只统计每个知识点已排定的下一次复习（FSRS 每复习一次才排出下一次）。需要重学的知识点列在最后。工作台里点一个已到期的条目，页面切到对话并发出 `/review 标题`，和自己输入一样直接开始复习；未到期的不能点，只读看板里都不能点。导航上的数字是逾期加今天的条目数。接口 `/api/schedule?view=week|month&date=YYYY-MM-DD`（参数非法时 400），不返回任务 ID。
- **学习记录**：每次自测或复习的题目、回答、分维度评分与引文、复习排期、状态迁移，以及相关 trace。

模型调用的详情默认只有元数据。想查看每次调用发了什么、回了什么，在 `.env` 设置 `ZHIQIU_TRACE_CONTENT=1`：之后的调用会把请求消息与回复正文存进 `llm_contents`（脱敏，请求最多 6 万字、回复最多 2 万字；`forget`、`chat forget` 一并删除）。这会把学习资料原文也存进库里，所以默认关闭。

看板只绑定 `127.0.0.1`、只接受 GET、以只读方式打开数据库（终端里的知秋可以同时运行），所有输出经过脱敏；页面不加载任何外部资源。看板显示的内容与 `zhiqiu trace` 相同，**不要把端口转发或暴露到本机以外**。

### 指标与花费

`zhiqiu metrics [--days 7] [--json]` 在读取时汇总：按阶段的调用次数、错误率、无效输出率、p50／p95 延迟、输入／缓存命中／输出 token 与花费；工具按状态的分布与重试率；工具循环结束原因；命令、对话与流程的结果分布；审批策略放行与用户批准次数（按工具）、人工确认次数（策略放行与本轮已允许不算询问）与被拒率、提议结果、人工改分、评分延期；各护栏的触发次数。库里只存 token，金额按 `ZHIQIU_PRICE_*` 在读取时计算（价格会变）；只统计，不设上限。

模型价格（9.2）按模型计算：每次调用按它自己的 `llm_calls.model` 和开始时间计价，再加起来，所以中途换过模型也算得对。一个模型的价格依次取：`price.models` 里按模型手填的（`ZHIQIU_PRICE_MODELS`）；`ZHIQIU_PRICE_*` 手填的价格（属于配置的模型 `ZHIQIU_LLM_MODEL`）；厂商价格页上读到的；都没有时，其他模型也用手填的那组价格（和 9.2 以前一样）。

`zhiqiu price update` 读一次模型端点厂商的价格页（目前认识 DeepSeek：`https://api-docs.deepseek.com/zh-cn/quick_start/pricing`），把正文交给当前模型做一次 JSON 抽取（一次小调用，记一条名为 `/price` 的 trace），读出每个模型的输入（缓存未命中）、缓存命中、输出价、货币、上下文长度，以及平峰价与高峰时段（例如 DeepSeek 工作日北京时间 9–12 点、14–18 点为高峰，其余时间半价）。抽取的结果不直接相信：模型名必须在页面原文里出现，价格是非负数，货币只能是 CNY 或 USD，高峰时段格式要对，不合格的丢弃并列出原因。结果连同页面地址、正文哈希和读取时间存到数据库旁边的 `prices.json`，同时记下端点 `GET /models` 返回的模型（模型按钮用）。工作台启动时，若价格超过 7 天没有更新就在后台读一次（`price.auto_update = false` 关闭；离线模式不读）。`zhiqiu price show` 列出可选模型现在的价格和来源。法定节假日按页面不能自动识别，仍按工作日的高峰计。金额以配置的 `price.currency` 显示（没配置时用价格页的货币）；货币不同的价格不计入合计，只在模型菜单里按它自己的货币显示。“指标与花费”页多了“按模型”一栏，`zhiqiu metrics --json` 的 `model.by_model` 给出每个模型的调用、token、花费和所用价格。

### 护栏留痕与脱敏

运行时护栏沿用 V3／V4，V5 让每次触发都写一条 `GuardrailTriggered` 事件（`rule`、`action`、`reason`，挂在当前 trace 上）：

| rule | 触发 | action |
|---|---|---|
| `leak_check` | 联网参数夹带本地资料原文或过长 | blocked |
| `read_root`／`write_root` | 读写白名单以外的路径、符号链接、`..` | blocked |
| `name_not_allowed`／`permission_denied` | 本步骤不允许的工具或权限类别（含 `--offline` 下的联网工具） | blocked |
| `confirmation_denied` | 用户拒绝了确认（含拒绝发送学习资料） | denied_by_user |
| `confirmation_unavailable` | 需要确认但当前环境无法确认 | blocked |
| `policy_denied` | 调用了审批策略设为 `deny` 的工具 | blocked |
| `web_request_refused` | 工作台写接口收到不合规的请求（缺令牌、外站 Origin、非 JSON、过大、非本机 Host） | blocked |
| `proposal_rejected`／`proposal_refused` | 用户拒绝提议／提议未通过执行前校验 | denied_by_user／blocked |
| `memory_limit` | 记忆 key 前缀或字数超限 | blocked |
| `loop_limit` | 工具循环以轮数、时限或预算上限结束 | stopped |
| `output_invalid` | 模型输出未通过校验（出题、评分、无效 JSON） | rejected |
| `illegal_transition` | 非法状态转换（被拒的改动已回滚，事件单独保留） | blocked |
| `consent_missing` | 资料或模型端点未获发送授权 | blocked |
| `question_shape` | 重出后的题目仍超出一题一问的形状限制，题目照样保存（V7） | warned |

密钥按**值**集中脱敏（`redact.py`）：已配置的 `ZHIQIU_LLM_API_KEY`、Exa 与 Notion 令牌的实际值，以及 `sk-…`、`Bearer …`、`ntn_…`、`secret_…` 格式，在写入工具审计、对话消息与错误、`mcp-audit.jsonl`、`llm_failures` 和看板输出前替换为 `[redacted]`；原有的按参数名脱敏保留。

### Eval：场景套件、运行契约与失败沉淀

- **运行契约检查**（`zhiqiu check [--days 7] [--json]`）：对已记录的运行逐项给出 PASS／FAIL／证据不足（没有可检查的数据时说“证据不足”，不当作通过），有 FAIL 时退出码为 1。检查项：工具循环轮数与结束原因（`loop_rounds`）、工具调用轮次在循环之内（`tool_round_bound`）、提议之后同一轮不再执行工具（`proposal_stops_round`）、写文件必须由用户批准、对话中的联网与外部 MCP 调用须经策略或用户批准（`confirmed_side_effects`；v19 之前没有来源的记录按用户批准计）、每次询问都有审批记录且结果与被决定的调用一致、没有超过 30 分钟仍未取消的待审批（`approvals_consistent`）、状态迁移按转换表重放且首尾衔接（`state_replay`）、模型与工具调用都挂在存在的 span 上（`trace_linked`）、没有遗留的运行中 span（`spans_closed`）、7.2 起每次讲解和澄清都记下了所用 Skill（`skills_recorded`）、8.0 起每次成功的流式模型调用都有不超过整次耗时的首字耗时（`stream_consistent`）。全部通过只说明程序守住了规则，不代表讲解或评分质量。
- **学习计划的运行契约**（10.0，`plans_consistent`）：进行中的计划里每天的新知识点不超过上限、前置单元排在前面、学完的单元都有完成的首次自测、推迟的单元都写了原因。
- **场景套件**（`tests/test_scenarios.py`）：用脚本化模型端到端运行学习→自测→评分、对话调用工具、提议→确认→流程→回到对话、自测中提问隔离、泄露拦截与确认被拒、中断与恢复，以及 V6 的默认策略（联网放行、写文件询问）、`deny` 配置、网页工作台上拒绝写文件、网页工作台上完成讲解与自测；每个场景结束后契约检查必须没有 FAIL，trace 必须是一棵连通的树。`tests/test_contracts.py` 用人为构造的违规记录检验每个检查器确实会判 FAIL。
- **失败分类**（`zhiqiu failures [--days 7] [--json]`）：模型错误、上下文溢出、无效输出（按阶段，不允许的追问单列 `invalid_followup`）、工具超时与错误、各护栏、工具循环上限、评分延期、流程中断与出错，每类附最近的样例 trace。用户有意拒绝（护栏动作为 `denied_by_user` 的 `confirmation_denied`、`proposal_rejected`）是用户的决定，不算失败：单独列在 `intentional` 里，不计入总数和看板导航上的失败数；`confirmation_unavailable`（无法确认）仍算失败（V7）。
- **失败沉淀**：输出未通过校验时，模型原文（脱敏、最多 2 万字）写入 `llm_failures`，`forget`／`chat forget` 一并删除。`zhiqiu failures export FAILURE_ID --to tests\regressions\NAME.json --expect rejected|recovered` 导出为回归样例，`tests/test_regressions.py` 把每个样例重放进同一套校验。第一条样例来自 V4 遗留的“不允许的追问”：V5 起出题校验失败会带着校验错误重试一次（`QUESTION_RETRIES=1`），重试给出合法题目即恢复。

### 发布门禁

`zhiqiu gate [--skip-tests] [--pytest-args "..."]`（只能在源码目录运行）：

1. 运行全量 pytest（含场景套件与回归样例），必须 100% 通过，不联网、不调用真实模型；
2. 计算评分提示指纹（出题、识别输入、各评分阶段的模板正文，共用的数据规则，`RUBRIC_VERSION` 与维度权重；`chat` 等其他模板的改动不影响指纹），要求 `labels/eval-runs.jsonl` 中有这个指纹的通过记录。

改了评分或出题提示却没有重跑人工标注，门禁就不通过，并提示运行 `examples/generate_label_samples.py --runs 1 --out <临时目录>`（约 30 次真实模型评分调用）。该脚本和 `zhiqiu evaluate --record` 会把结果连同指纹、提交、模型、一致率、token 与花费追加到 `labels/eval-runs.jsonl`。门禁本身从不调用真实模型。

## 验证与诊断

```powershell
uv run --python 3.12 --extra test python -m pytest -q
```

测试覆盖迁移与回滚、会话恢复、幂等任务、只读提醒、上下文预算与审计、记忆失效、多题评分与证据校验、待评阅恢复、长答分段、旧讲解排序、工具层（校验、闸门、重试、总时限、审计）、诊断逻辑，以及 V5 的 trace、指标、护栏事件、脱敏、运行契约、端到端场景、回归样例、门禁和看板接口，8.0 的 FastAPI 接口与 SSE 事件流（`test_web_stream.py`）、流式解析（`test_model_stream.py`，用 httpx 的 MockTransport）、对话流式与进度（`test_chat_stream.py`）、首字耗时（`test_ttft.py`）；测试默认 `ZHIQIU_LLM_STREAM=0`，流式测试自己打开；均使用脚本化模型，不需要密钥。发布前运行 `uv run zhiqiu gate`。

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

## 学习计划的观测（10.0）

- trace：`plan.draft`（生成草稿）、`plan.revise`（一起调整的每一轮）、`plan.web_outline`（联网参考大纲）、`plan.activate`（确认）、`plan.recompute`（日期有变化的每日重排，属性里有改了几个单元和原因）；草稿和修改用的模型调用是上下文任务 `plan`。
- 事件：`PlanDrafted`、`PlanRevised`、`PlanActivated`、`PlanRescheduled`、`UnitDone`、`UnitBlocked`、`UnitUnblocked` 会点亮实时页的“排程与计划”框，其余计划事件只进列表。
- 指标（`metrics` 与看板“指标”页的“学习计划”一组，不受天数范围影响）：各状态的计划数、完成率（完成 ÷ 结束）、学完的知识点数、准时率（学完日期不晚于最初排定的日期）、平均延期天数、实际 ÷ 估计用时的中位数、自动重排次数及其中因复习日而顺延的次数。
- 护栏 `plan_limit`：计划的知识点超过上限，或一份计划讨论超过 12 轮。草稿不合规的模型原文照常进 `llm_failures`，可以导出为回归样例。
