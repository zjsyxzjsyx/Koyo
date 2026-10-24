# Zhiqiu (知秋)

[中文](README.md) | English

Zhiqiu is a learning agent that runs on your own machine. Point it at a topic and your material (notes, a code repository, web pages or Notion pages): it teaches the topic in short sections, quizzes you Feynman-style when you are done, grades the answers dimension by dimension, and schedules reviews with FSRS. For a larger subject it drafts a study plan that spreads the topic over days. Day to day you simply chat with it; when it suggests a lesson, a review or a plan, nothing starts until you confirm.

> The interface, the prompts and the teaching are in **Chinese only** for now.

## Design

The model teaches, asks and judges; the program decides.

- **The model proposes, the program acts.** In chat the model can only *propose* a lesson, a review, a plan, a memory or a reminder; the user confirms and the program runs it. Grading totals, review dates and plan dates are computed by code, never by the model.
- **Explicit state.** Knowledge points, attempts, review tasks, lessons, conversations and study plans move through declared transition tables. Every move is an event, so a run can be replayed and checked.
- **Bounded tool loop.** Chat is a ReAct loop with a cap on rounds, time and budget. Tools are declared, validated and audited in one layer. Network, external MCP and file writes go through an approval policy (allow, ask or deny). Paths outside the allow-list are asked for in the conversation.
- **Grounded grading.** Every score quotes the learner's original answer verbatim. The program checks the quotes and the coverage of every question before it computes the total (50/30/20 over concept, mechanism and application).
- **Study plans.** The model sees only the paths and headings of your material, never the text, and proposes units. The program checks the draft (cycles, limits, missing sources, points already learned) and dates it: a daily cap on new units shared across plans, review-heavy days kept free, missed days pushed back as a whole, and units held back while a prerequisite scored below 50.
- **Observability and governance.** Every turn is a trace (a span tree written as it runs). A local dashboard shows live progress, waterfalls, metrics and cost. Guardrail hits are recorded and secrets are redacted by value. Run contracts (`zhiqiu check`) verify the rules on real records. Failed model outputs become regression cases, and `zhiqiu gate` is the release gate.
- **Graceful degradation.** Zhiqiu works with any OpenAI-compatible Chat Completions endpoint. When an endpoint refuses `stream`, `tools`, `thinking` and the like, Zhiqiu drops the field, retries and remembers. Context is assembled per stage within a token budget, and older chat turns are summarized.

## Features

- Lessons in 3–5 sections, with optional flow charts, trees or concept maps drawn as SVG; one-question-at-a-time self-tests; FSRS reviews.
- Material: local notes and code (`.gitignore` respected), web pages, and Notion pages. What will be sent, its size and its estimated cost are listed before anything is sent.
- Web workbench (`zhiqiu web`, FastAPI and SSE): streamed answers, a status panel, a review calendar, plan cards, and approval cards.
- Local voice input (SenseVoice via sherpa-onnx, offline; recordings are never stored).
- One user-level `config.toml` (`zhiqiu config init`).

## Quick start

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```powershell
uv sync --python 3.12
uv run zhiqiu config init      # endpoint, model, API key, readable folders
uv run zhiqiu                  # opens the web workbench
```

To try it without a model or a key, run `uv run python examples/demo.py` (a fixed clock and a scripted model).

Common commands:

```powershell
uv run zhiqiu learn 'Binary tree traversal' --from .\notes.md
uv run zhiqiu plan new 'Redis' --from D:\notes\redis
uv run zhiqiu review
uv run zhiqiu status
uv run zhiqiu dashboard --open
uv run zhiqiu check --days 7
uv run zhiqiu gate
```

## Data and privacy

- Everything stays in a local SQLite file (by default `%LOCALAPPDATA%\zhiqiu\state.sqlite`). `zhiqiu export --json` exports it, and `forget` deletes data.
- Only material you confirmed, the conversation itself and files you allowed are sent to the model endpoint. Web search sends only the query.
- Material, web pages, tool results and memories are marked as data in every prompt, and instructions inside them are not followed. The model has no tool that can change the allow-list or the approval policy.

## Development

```powershell
uv sync --python 3.12 --extra test --extra mcp
uv run python -m pytest -q     # scripted models, no keys needed
uv run zhiqiu gate
```

The detailed documentation is in Chinese under [`docs/`](docs/), starting with [architecture](docs/architecture.md), [study plans](docs/plans.md) and [observability](docs/observability.md).
