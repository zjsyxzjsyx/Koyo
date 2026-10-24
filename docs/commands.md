# 命令

全部命令与会话内的斜杠命令。返回 [README](../README.md)。

## 命令

默认数据库位于操作系统用户数据目录的 `zhiqiu/state.sqlite`，也可在配置文件里用 `web.db` 或全局 `--db PATH` 指定（9.0）。

```powershell
uv run zhiqiu config init
uv run zhiqiu config show
uv run zhiqiu config get web.port
uv run zhiqiu config path
uv run zhiqiu
uv run zhiqiu voice install
uv run zhiqiu check-model --capabilities
uv run zhiqiu check-model --reset
uv run zhiqiu --db .\state.sqlite learn '二叉树遍历' --from .\notes.md
uv run zhiqiu --db .\state.sqlite learn 'FastAPI 依赖注入' --from D:\code\demo-api
uv run zhiqiu --db .\state.sqlite learn '二叉树遍历' --url https://en.wikipedia.org/wiki/Tree_traversal
uv run zhiqiu --db .\state.sqlite learn '二叉树遍历' --notion https://www.notion.so/你的页面-<32位页面ID>
uv run zhiqiu --db .\state.sqlite chat
uv run zhiqiu --db .\state.sqlite chat --resume CONVERSATION_ID
uv run zhiqiu --db .\state.sqlite chat list
uv run zhiqiu --db .\state.sqlite chat log CONVERSATION_ID
uv run zhiqiu --db .\state.sqlite chat forget CONVERSATION_ID
uv run zhiqiu --db .\state.sqlite web --open
uv run zhiqiu --db .\state.sqlite status
uv run zhiqiu --db .\state.sqlite resume
uv run zhiqiu --db .\state.sqlite review
uv run zhiqiu --db .\state.sqlite plan new 'Redis' --from D:\notes\redis --goal '下周面试'
uv run zhiqiu --db .\state.sqlite plan list
uv run zhiqiu --db .\state.sqlite plan show 1
uv run zhiqiu --db .\state.sqlite plan today
uv run zhiqiu --db .\state.sqlite plan pause 1
uv run zhiqiu --db .\state.sqlite plan resume 1
uv run zhiqiu --db .\state.sqlite plan abandon 1
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
uv run zhiqiu --db .\state.sqlite skills
uv run zhiqiu voice check
uv run zhiqiu --db .\state.sqlite voice eval .\voice-samples
```

会话内 `/done` 确认学完并开始自测，`/pause` 保存问答进度，`/end` 明确结案，`/sources web|local` 纠正资料选择，`/accept N` 接受关联知识点建议，`/question 文本` 记录并解释澄清问题；讲解阶段直接输入不以 `/` 开头的文字也按澄清问题处理（以 `/` 开头但不认识的输入仍提示“未知命令”）。直接关闭或异常退出不会自动结案。讲解、自测与复习的输入循环在 `flows.py` 中，CLI 命令与对话模式共用。自测和复习中，`/answer 文本` 直接作为回答保存，`/question 文本` 直接作为提问；不带前缀的输入先由模型判断是回答还是提问（`classify_turn`，拿不准时判为提问）：判为回答直接保存；判为提问时请用户确认（回车确认，输入 `a` 改为回答），确认的提问按澄清处理、记为不计入评分的笔记（事件标记 `via=classified`），评分请求看不到它；判断失败时请用户选择 `a` 或 `q`。不支持判断的脚本化模型按回答保存。`--offline` 禁止 Web 补充，也不能使用 `--url`、`--notion`。`--url`、`--notion` 可重复，可与 `--from` 同时使用，三者至少给出一个；每个网页最多取 15,000 字，超出部分截断并在来源说明中注明，本地、网页与 Notion 资料合计不超过 `materials.max_chars`（默认 500,000 字，见[资料类型与上限](learning.md#资料类型与上限90)）。网页或 Notion 页面获取失败时学习停止，不保存任何资料。低分维度关联的原题原答会成为后续复习出题的重点。

学习计划（10.0）：对话里 `/plan 主题 [--from 路径] [--url 网址] [--notion 页面]` 制定计划，`/plans` 列出计划，`/plan 编号` 打开一份，`/today` 看今天的安排，`/today start [编号]` 按计划开始学习，`/today ahead` 在今天的计划学完、时间还有余量时提前学下一个。计划卡片上的终端操作见[学习计划](plans.md#草稿卡片)。
