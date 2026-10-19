# 知秋

知秋是本地运行的学习 Agent。你指定一个知识点和资料（笔记、代码仓库、网页或 Notion 页面），知秋分节讲解，学完后用费曼式问题自测；程序保存原题、原答、分维度评阅和复习任务，并按 FSRS 安排复习。平时可以直接和知秋对话，需要时由它建议、你确认后进入学习、自测或复习。模型负责教学、出题与语义评阅；业务写入、状态转换、总分计算和排期由程序执行。

## 功能

- **学习与复习**：讲解分 3–5 节逐节显示，可配流程图、树或概念图；自测一题一问，按维度评阅并引用原答逐字证据，程序核对后计算总分、按 FSRS 排期。
- **资料**：本地笔记与代码（含配置文件与 Jupyter 笔记本，自动跳过依赖、构建目录和 `.gitignore` 里的文件），网页（Exa），Notion 页面；发送前一次列出要发送的内容、规模与预计花费，确认后才发出请求。
- **对话**：有界的工具循环，可以查学习状态、读写笔记、联网搜索、调用外部 MCP 工具；读写白名单外的文件时在对话里申请授权（只允许这一次、加入白名单或拒绝）。
- **网页工作台**：`zhiqiu web` 在浏览器里对话和学习，回答逐字出现；右侧状态面板显示目标、上下文范围、执行状态、风险动作与验证结果；会话侧栏、复习日程、目录白名单、审批页。
- **本地语音**：按住麦克风说话，本机 SenseVoice 离线识别，录音不保存；模型第一次使用时自动下载。
- **观测与治理**：每轮一条 trace，看板显示实时进度、瀑布图、指标与花费；护栏留痕、密钥脱敏、运行契约检查、失败沉淀为回归样例、发布门禁。
- **兼容与兜底**：任何 OpenAI 兼容的 Chat Completions 端点；端点不支持流式、工具调用等字段时自动去掉重试并记住，其他错误给出中文说明。
- **配置**：一个用户级 `config.toml` 放 API Key 和默认启动项，`zhiqiu config init` 交互生成。

## 快速开始

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。在项目目录运行：

```powershell
uv sync --python 3.12
uv run zhiqiu config init
uv run zhiqiu
```

1. `uv sync` 安装依赖（包括语音输入的依赖）。
2. `zhiqiu config init` 逐项询问模型端点、模型名、API Key（输入不显示）、可读目录和启动方式，写入 `%APPDATA%\zhiqiu\config.toml`；不想用配置文件时，环境变量和项目目录的 `.env` 照样可用，且优先于它。
3. 之后运行 `zhiqiu` 就打开网页工作台（选了“只看状态”时进入终端对话）。

想先不连模型看看效果：`uv run python examples/demo.py`（固定时钟、脚本化模型，不需要 API Key）。

Windows 桌面入口：运行一次 `powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1`，桌面会出现“知秋工作台”快捷方式，双击启动工作台并用 Edge 独立窗口打开。

## 常用命令

```powershell
uv run zhiqiu                                   # 按配置打开工作台，或显示状态并进入对话
uv run zhiqiu web --open                        # 网页工作台
uv run zhiqiu chat                              # 终端对话
uv run zhiqiu learn '二叉树遍历' --from .\notes.md
uv run zhiqiu learn 'FastAPI 依赖注入' --from D:\code\demo-api
uv run zhiqiu learn '二叉树遍历' --url https://en.wikipedia.org/wiki/Tree_traversal
uv run zhiqiu review                            # 开始到期复习
uv run zhiqiu status
uv run zhiqiu config show                       # 每项配置的取值与来源（密钥打码）
uv run zhiqiu check-model --capabilities        # 探测端点支持哪些功能
uv run zhiqiu voice install                     # 下载语音模型
uv run zhiqiu dashboard --open                  # 只读看板
uv run zhiqiu metrics --days 7
uv run zhiqiu gate                              # 发布门禁
```

全部命令和会话内的斜杠命令见 [docs/commands.md](docs/commands.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [安装与配置](docs/configuration.md) | 依赖、`config.toml` 与 `.env`、全部设置项、模型预算、端点兼容与报错 |
| [学习、自测与复习](docs/learning.md) | 讲解方式、教学 Skill、出题与输入、评阅显示、状态机、资料类型与上限、评分规则、跨天追问 |
| [对话、工具与审批](docs/chat.md) | 对话模式、工具规则、审批策略、对话内授权、历史检索、工具层 |
| [网页工作台](docs/workbench.md) | 页面、会话侧栏、目录白名单、接口表、桌面入口、服务层与安全、流式输出 |
| [语音输入](docs/voice.md) | 自动安装、使用方式、隐私、准确率自测 |
| [Notion 与 MCP](docs/integrations.md) | Notion 页面资料、只读 MCP 服务、外部 MCP 工具 |
| [观测、评测与门禁](docs/observability.md) | Trace、看板、指标与花费、护栏与脱敏、Eval、发布门禁、验证与诊断 |
| [命令](docs/commands.md) | 全部命令与斜杠命令 |
| [功能与结构](docs/architecture.md) | 按版本的功能概览、各模块职责 |
| [已知限制](docs/limitations.md) | 目前已知的限制与取舍 |

## 数据与隐私

- 数据库默认在 `%LOCALAPPDATA%\zhiqiu\state.sqlite`（类 Unix 为 `~/.local/share/zhiqiu/state.sqlite`），可用 `web.db` 或 `--db` 指定；`zhiqiu export --json` 导出，`forget` 与 `chat forget` 删除。
- 发给模型端点的只有你确认过的资料、对话内容和授权过的文件；联网搜索只发送查询词，不含资料原文。
- 资料、网页正文、工具结果与记忆在所有提示里都声明为数据，其中的指令不执行；模型没有任何工具能改白名单或审批策略。
- 密钥只存在本机的 `config.toml`、`.env` 或环境变量里，写入日志、trace、审计和导出前按值脱敏。
- 语音只在本机识别，录音不写盘。

## 开发

```powershell
uv sync --python 3.12 --extra test --extra mcp
uv run python -m pytest -q
uv run zhiqiu gate
```

测试使用脚本化模型，不需要密钥。真实服务的冒烟脚本在 `examples/live_*.py`，会产生少量费用。版本历史与设计记录见各版本的 PRD。
