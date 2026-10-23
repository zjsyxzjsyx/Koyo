# Notion 与 MCP

Notion 页面资料、只读 MCP 服务和外部 MCP 工具。返回 [README](../README.md)。

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

[server.json](../server.json) 按官方 MCP Registry 的 2025-12-11 格式登记本服务（PyPI 包 `zhiqiu`，经 `uvx --from zhiqiu[mcp]==9.1.0` 以 stdio 启动），只用于本地记录和以后发布，目前没有发布到 Registry 或 PyPI。测试会用 `tests/data/server.schema.json`（官方 schema 的本地副本）离线校验它，并核对版本号与 `pyproject.toml` 一致。

## 外部 MCP 工具（客户端）

配置 `ZHIQIU_MCP_CONFIG` 指向一个 JSON 文件后，`learn` 会启动其中列出的本地 stdio MCP 服务，把白名单内的工具接入工具层，供资料准备的工具循环和对话模式使用（对话中按审批策略，默认每次调用前确认；学习阶段澄清、自测和复习不开放外部工具）。示例见 [examples/mcp.example.json](../examples/mcp.example.json)（官方 `mcp-server-fetch`）：

```json
{"servers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"],
  "tools": {"fetch": {"permission": "external_mcp", "timeout_s": 60, "max_output_chars": 6000}}}}}
```

- 只注册 `tools` 白名单里且服务端确实提供的工具，名字为 `<服务名>__<工具名>`；与已有工具重名时报错并写明两边来源。权限只能是 `read`、`network` 或 `external_mcp`（默认）。
- 学习开始的统一确认会按服务列出可能调用的外部工具。外部工具的参数同样经过泄露检查，返回内容作为补充资料（`web`），不计入发送授权，由程序重新判断资料是否充分。
- 返回 `isError` 时记为 `upstream`；超过 `timeout_s` 时取消这次请求并记为 `timeout`；某个服务启动失败时跳过它，并在 `tool_calls` 记一条 `<服务名>__*` 的失败记录，其余服务照常使用。服务的标准错误输出写到数据库目录下的 `mcp-servers.log`。
- 需要可选依赖 `mcp`；离线模式（`--offline`）不启动外部服务。
