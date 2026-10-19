# 安装与配置

依赖、配置文件、环境变量、模型预算与端点兼容。返回 [README](../README.md)。

## 安装与配置

需要 Python 3.12 和 `uv`。以下命令在包含 `pyproject.toml` 的项目目录运行：

```powershell
uv sync --python 3.12 --extra test
# 需要 MCP 服务或 MCP 客户端工具时：
uv sync --python 3.12 --extra test --extra mcp
uv run --python 3.12 python examples/demo.py
```

演示使用固定时钟和脚本化模型，不需要 API 密钥。

9.0 起语音输入的依赖（sherpa-onnx、numpy、pypinyin）随 `uv sync` 一起装上，`--extra voice` 仍可写但不再需要；语音模型在第一次使用时下载（见[语音输入](voice.md)）。

## 配置文件 config.toml（9.0）

所有设置都可以写进一个用户级的配置文件，不用每次设环境变量：

- 位置：Windows 为 `%APPDATA%\zhiqiu\config.toml`，类 Unix 为 `$XDG_CONFIG_HOME/zhiqiu/config.toml`（没设时 `~/.config/zhiqiu/config.toml`）。`zhiqiu config path` 打印实际位置；全局 `--config 路径` 或环境变量 `ZHIQIU_CONFIG` 可以换一个文件。
- 生成：`zhiqiu config init` 逐项询问模型端点、模型名、API Key（输入不显示，已有的直接回车沿用）、可读目录、工作台端口、不带参数运行 `zhiqiu` 时是否打开工作台、是否自动打开浏览器；项目目录里已有 `state.sqlite`（桌面快捷方式一直用它）时会问以后是否都用它。已有文件先另存为 `config.toml.bak`，手改过的其他项保留。写完后在 Windows 上把文件权限收紧为只有当前用户可读，最后可以顺便下载语音模型、检查模型联通。
- 查看：`zhiqiu config show` 列出每一项的实际取值和来源（环境变量、`.env`、`config.toml` 或默认），密钥只显示末 4 位；`--json` 给脚本用；`zhiqiu config get web.port` 输出一项的值（密钥不输出）。
- 优先级：进程环境变量 > 项目目录的 `.env` > `config.toml` > 默认值；命令行参数（如 `--port`、`--db`、`--offline`）又优先于这些。
- 校验：未知的节或键、类型不对（例如 `port = "8765"`）都会报错并写明文件和键名，不会悄悄忽略；报错里不出现任何值。

各节的键与注释见仓库根目录的 [`config.example.toml`](../config.example.toml)（由代码里的设置表生成，测试保证两者一致）；同样的设置写成环境变量的版本见 [`.env.example`](../.env.example)。主要的节：

| 节 | 内容 |
|---|---|
| `[model]` | `base_url`、`model`、`api_key`、`stream`、窗口与预算（`window`、`output_reserve`、`margin`、`budgets`） |
| `[web]` | `port`（默认 8765）、`open`（启动后打开浏览器）、`db`（数据库文件，留空用 `%LOCALAPPDATA%\zhiqiu\state.sqlite`）、`default_command`（`web` 或 `status`） |
| `[learn]` | `offline`：相当于每次都加 `--offline` |
| `[materials]` | 学习资料的上限与类型：`max_chars`、`max_file_bytes`、`max_files`、`code_files`、`extra_suffixes`、`respect_gitignore`（见[学习](learning.md#资料类型与上限90)） |
| `[roots]` | `read`、`write`（对话中的可读、可写目录，与工作台页面添加的合并）、`skills`（自己的教学 Skill 目录） |
| `[voice]` | `auto_download`（`ask`、`always`、`never`）、`mirror_url`、`model`、`language`、`threads` |
| `[services]` | `exa_api_key`、`notion_token`、`mcp_config`、`approval_config` |
| `[price]` | `input`、`cached_input`、`output`、`currency` |
| `[trace]` | `content`：保存每次模型调用的请求与回复正文 |

默认启动项：`config init` 生成的文件里 `default_command = "web"`，之后在任意目录运行 `zhiqiu` 就按 `[web]` 打开工作台；没有配置文件时与 8.0 相同（显示状态，交互终端里进入对话）。`zhiqiu web` 的 `--port`、`--open/--no-open` 不写时取 `[web]` 的值。桌面快捷方式（`scripts\zhiqiu-web.ps1`）也读同一份配置：端口取 `web.port`；数据库先看 `web.db`，没写而项目目录里有 `state.sqlite` 时继续用它，否则用默认位置。

## 环境变量与 .env

CLI 自动读取项目根目录的 `.env`（已被 Git 忽略）；已设置的进程环境变量优先，`.env` 又优先于 `config.toml`。至少需要（任选一种方式提供）：

```powershell
$env:ZHIQIU_LLM_BASE_URL = 'https://your-provider.example/v1'
$env:ZHIQIU_LLM_MODEL = 'your-model'
$env:ZHIQIU_LLM_API_KEY = 'your-key'
```

可选配置（都可以写进 `.env` 或 `config.toml`，`ZHIQIU_LONG_ANSWER_SEGMENTS` 除外）：

| 变量 | 作用 | 不配置时 |
|---|---|---|
| `ZHIQIU_EXA_API_KEY` | Exa 联网搜索补充资料、`learn --url` 获取网页正文 | 不能联网补充，不能用 `--url` |
| `ZHIQIU_NOTION_TOKEN` | `learn --notion` 读取 Notion 页面（Notion 内部集成令牌，设置见“Notion 页面资料”） | 不能用 `--notion` |
| `ZHIQIU_WRITE_ROOTS` | 学习中澄清和对话时允许写笔记的目录，多个用 `;` 分隔，例如 `D:\notes\zhiqiu`；工作台“目录白名单”页和对话里授权加入的目录与它合并 | 只用页面和对话里加入的；都没有时写到其他位置要在对话里逐次授权 |
| `ZHIQIU_READ_ROOTS` | 对话中允许直接读取的目录（白名单），多个用 `;` 分隔；也是对话中 `propose_learn` 可指定资料的范围；页面和对话里加入的目录与它合并 | 只用页面和对话里加入的；读其他位置的文件要在对话里授权 |
| `ZHIQIU_SKILLS_DIR` | 自己的教学 Skill 目录（每个子目录一个 `SKILL.md`，见“教学 Skill”）；不能与可写目录重叠 | `%LOCALAPPDATA%\zhiqiu\skills`（类 Unix 为 `~/.local/share/zhiqiu/skills`），不存在就只用内置 Skill |
| `ZHIQIU_VOICE_MODEL` | 语音模型目录（含 `model.int8.onnx`、`tokens.txt`） | `%LOCALAPPDATA%\zhiqiu\voice\sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17` |
| `ZHIQIU_VOICE_LANGUAGE` | 识别语言：`auto`、`zh`、`en`、`ja`、`ko`、`yue` | `auto` |
| `ZHIQIU_VOICE_THREADS` | 识别用的 CPU 线程数 | `min(4, CPU 核数)` |
| `ZHIQIU_VOICE_AUTO_DOWNLOAD`、`ZHIQIU_VOICE_MIRROR_URL` | 缺少语音模型时是否下载（`ask`、`always`、`never`）、从哪里下载 | 第一次用时先问；从 GitHub 下载 |
| `ZHIQIU_MCP_CONFIG` | 外部 MCP 工具配置文件，例如 `D:\code\知秋\知秋V1.0\examples\mcp.example.json` | 不启动外部 MCP 服务 |
| `ZHIQIU_APPROVAL_CONFIG` | 对话审批策略文件（见“审批策略”） | 联网自动执行，外部 MCP 与写文件每次询问，白名单外路径每次申请 |
| `ZHIQIU_MODEL_BUDGETS` | 按模型配置窗口、输出预留与余量（见下文） | 沿用下面三个变量 |
| `ZHIQIU_LLM_WINDOW`、`ZHIQIU_LLM_OUTPUT_RESERVE`、`ZHIQIU_LLM_MARGIN` | 未在 `ZHIQIU_MODEL_BUDGETS` 中匹配的模型的预算 | 不做超限判断 |
| `ZHIQIU_LLM_WINDOW_VERIFIED` | 设为 `1` 表示窗口已实测（只记入审计） | 视为未实测 |
| `ZHIQIU_LLM_STREAM` | 设为 `0` 关闭流式请求（回到 7.2 的一次性返回）；端点不支持流式时自动退回，见下方“端点兼容” | 开启 |
| `ZHIQIU_LONG_ANSWER_SEGMENTS` | 设为 `1` 开启长答分段评阅（只能用进程环境变量） | 关闭 |
| `ZHIQIU_PRICE_INPUT`、`ZHIQIU_PRICE_OUTPUT` | 每百万输入、输出 token 的价格，用于 `metrics`、看板与 trace 中的花费 | 只显示 token，不显示金额 |
| `ZHIQIU_PRICE_CACHED_INPUT` | 每百万缓存命中输入 token 的价格（如 DeepSeek 的缓存命中价） | 按输入价计 |
| `ZHIQIU_PRICE_CURRENCY` | 金额前缀，例如 `¥` 或 `$` | 不显示币种 |
| `ZHIQIU_TRACE_CONTENT` | 设为 `1` 时保存每次模型调用的请求与回复正文（脱敏、截断），供看板查看 | 只保存元数据 |

`[web]`、`[learn]`、`[materials]` 等新设置对应的变量（如 `ZHIQIU_WEB_PORT`、`ZHIQIU_DB`、`ZHIQIU_OFFLINE`、`ZHIQIU_MATERIALS_MAX_CHARS`）见 `.env.example`。`.env` 只接受 `config.toml` 能设置的这些变量；出现其他变量名会报错，避免拼错的设置被悄悄忽略。

模型端点须支持 OpenAI 风格的 Chat Completions；Web 补充还须支持工具调用。开始学习前，CLI 一次列出将发送的本地文件、要经 Exa 获取的网页、要读取的 Notion 页面、本次是否可能联网搜索或调用外部 MCP 工具，以及模型端点；用户确认后才发出任何请求，拒绝则一个请求也不发。Web 搜索只发送查询字符串；密钥不写入数据库或导出文件。发送授权按本地文件、网页、Notion 等用户指定资料的内容计算，任一资料变化后需要重新 `learn`；联网搜索补充的结果不计入授权。所有阶段模板都声明：资料、网页正文、工具结果与个人记忆只是数据，其中的指令一律不执行。

模型窗口与预算用 `ZHIQIU_MODEL_BUDGETS` 按模型配置，键为模型名或 `端点|模型名`（后者优先），例如：

```json
{"https://provider.example/v1|model-a": {"window": 131072, "output_reserve": 32768, "margin": 4096, "verified": false, "validation_retries": 2}}
```

输入上限为 `window − output_reserve − margin`，`output_reserve` 同时作为请求的 `max_tokens`。推理型模型的输出包含推理内容，输出预留应留足。未匹配的模型沿用 `ZHIQIU_LLM_WINDOW`、`ZHIQIU_LLM_OUTPUT_RESERVE`、`ZHIQIU_LLM_MARGIN`；缺少窗口参数时不做超限判断。`verified` 是本地配置声明，只记入审计。

上下文预算优先复用服务端报告的 `usage.prompt_tokens`：新请求完整保留某次已测量请求的消息前缀时，以报告值为基数估算新增部分；否则按 UTF-8 字节保守估算。估算方法与结果都写入请求审计。

## 端点兼容与报错（9.0）

兼容 OpenAI 的服务支持的字段各不相同。端点拒绝请求里的某个可选字段时，知秋去掉这个字段再发一次，而不是直接报错：

- 能识别的字段：`stream_options`、`stream`、`tools`（同时去掉 `tool_choice`）、`tool_choice`、`thinking`、`max_tokens`（改名为 `max_completion_tokens` 再发）。
- 识别方法：状态码为 400、403、404、405、415、422、501（或 5xx），报错正文同时提到这个字段名和 unsupported、unknown、extra、not permitted、不支持之类的词。只认请求里真的带了的字段；上下文溢出照旧单独判断。
- 报错点名了字段、去掉后重试成功的，按“端点 + 模型”记进数据库（schema v24 的 `endpoint_capabilities`），以后启动也不再发送这个字段，不会每次先失败一次；同时在对话里说一句“模型端点不支持……已记住”，并记护栏 `endpoint_feature_disabled`。报错没点名字段（流式时的一般 400）只在本进程里退回非流式，和 8.0 一样。
- 不支持工具调用的模型：对话照常以纯文本回答，不能联网、读写文件或提议学习；上下文里写明这一点，工作台状态面板显示“端点不支持工具调用”；需要学习时直接用 `/learn`。
- 其他错误给中文说明，不再显示 httpx 的英文异常：401（Key 无效）、402（余额不足）、403（没有权限）、404（端点地址或模型名不对，附实际地址）、429（太频繁或额度用完，先按 `Retry-After` 等一次再重试，最多 10 秒）、5xx、超时与连不上。trace 的 `error_kind` 记具体类别（`auth`、`not_found`、`rate_limited`、`timeout`、`connect` 等）。

`zhiqiu check-model --capabilities` 用公开合成文本分别探测流式、工具调用、`tool_choice` 与（DeepSeek 的）`thinking`，约 4 次很小的调用，重新探测前先清掉旧记录，结果写库并打印；`zhiqiu check-model --reset` 只清除记录，不调用模型。
