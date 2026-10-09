# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.
- 关于策略的py文件如果有新建机制，则新建py文件。如果只是修改参数、优化机制、小改动等，不涉及新增机制的变更则在原有策略上改动。
- 新建策略的不要import原来同级别的其他版本策略文件。要完全新建。但是可以import 复用的config/core/util等公用的文件。
- 声明变量时，不要一行多个变量。要便与阅读。

## 策略研究记录约定

- 本约定适用于 `Stragety/` 下每一种可独立比较版本的策略类型。
- 新增研究方向、变更策略机制、调整参数或形成新版本时，必须同步更新该策略类型目录中的 `StrategicResearchDirectionsAndEffectivenessRecords.md`。
- 尚无记录文件的策略类型，在下一次发生研究或变更时，按 `Stragety/STRATEGY_RESEARCH_RECORD_RULES.md` 创建；不为没有研究活动的目录批量创建空文件。
- 通用记录规则、状态定义、最佳版本判定和记录模板以 `Stragety/STRATEGY_RESEARCH_RECORD_RULES.md` 为唯一来源。各类型记录文件只保存当前结论和研究记录，不复制通用规则。
- 未提供统一口径的可复现证据时，不得把版本标记为“最佳”。失败、无效和不可比的研究也必须记录。

## Repository Purpose

### 策略回测数据周期约定

- 策略进行回测时，默认使用 1 分钟级别的 K 线数据。

### DayT 固定回测约定


## Architecture

### Directory Layout

```
MyPy-Q/          ← QMT实盘策略 (active work)
  QMT_迷你反T_v5_动态买回_实盘策略.py   ← current latest: 长飞光纤 T0 反T
  QMT_迷你反T_v3/v4/v2_*.py           ← prior versions
  长飞光纤_日内做T策略.py              ← original backtest+live hybrid
  Alpha144_*.py                       ← liquidity impact strategies
backtest/        ← 离线回测引擎 (runs QMT-format strategies without QMT)
  engine.py      ← BacktestEngine: cash, positions, equity curve, order simulation
  config.py      ← backtest params (capital, risk, stock pool)
  data_source.py ← fetches OHLCV from Tencent/akshare APIs
  qmt_mock.py    ← mocks passorder/get_trade_detail_data/get_history_data etc.
  _xtquant_mock/ ← mocks xtdata/xttrader SDK
  okh/           ← enhanced engine (OskhQuant adapter)
  run_okh.py     ← CLI: python -m backtest.run_okh --strategy "MyPy-Q/xxx.py"
analysis/        ← research reports (601869 T0, supply chain, sector analysis)
StockPickingStrategy/ ← multi-stock selection strategies v1→v6
demo-py/         ← QMT API usage examples
references/md/   ← QMT官方文档 Markdown 提取 (686 pages → structured .md)
docs/            ← user-facing tutorials
```

### Strategy Version Evolution (601869 T0)

The QMT live strategies evolved through progressive refinement:

| Version | File | Key Feature |
|---------|------|------------|
| v1 (original) | `长飞光纤_日内做T策略.py` | Backtest + live hybrid, 4 sub-strategies, anti-sell-protection |
| v2 | `QMT_迷你反T_v2_冲高回落_实盘策略.py` | State machine (IDLE→SPIKING→SOLD→DIPPING), 冲高回落确认 |
| v3 | `QMT_迷你反T_v3_高价股_实盘策略.py` | Adapted for 464元 share price (smaller PULLBACK%/BOUNCE%) |
| v4 | `QMT_迷你反T_v4_优化版_实盘策略.py` | `is_last_bar()` historical noise filter, adaptive ATR multiplier |
| **v5 (latest)** | `QMT_迷你反T_v5_动态买回_实盘策略.py` | Buyback trigger based on **actual sell price** (not fixed daily calc) |
-命名规则:{策略名}_{版本}_{变更点/优化点}
### Strategy State Machine (v5)

```
IDLE → SPIKING (price ≥ trigger, tracking peak) → SOLD (pullback confirmed)
                                                      ↓
                                    price ≤ sell_price×(1-ATR%×0.15) → DIPPING
                                                      ↓                    ↓
                                            emergency (price ≥ sell+2%)  bounce ≥ 0.10%
                                                      ↓                    ↓
                                                  买回(紧急)           买回(正常) → DONE
```

### QMT Runtime Context

QMT strategies run inside the QMT client, which provides injected globals:
- `init(ContextInfo)` — called once on strategy load
- `handlebar(ContextInfo)` — called each bar (K-line period)
- `ContextInfo.st` — user-defined state dict (persists across calls)
- `ContextInfo.run_time("funcName", "3nSecond", ...)` — timer for intraday ticks
- `ContextInfo.is_last_bar()` — True for current bar (not historical replay)
- `get_trade_detail_data(account, 'STOCK', 'POSITION'/'ACCOUNT')` — position/cash queries
- `order_shares(code, ±shares, 'FIX', price, ContextInfo, account)` — place orders
- `ContextInfo.get_full_tick([code])` — real-time bid/ask/last price
- `ContextInfo.get_history_data(N, '1d', 'close')` — {code: [values]} dict

Python strategy source files use UTF-8 (`# -*- coding: utf-8 -*-`). Do not convert
QMT or MiniQMT strategy files to GBK unless the user explicitly requests it for a
specific external runtime.

## Current State (July 2026)

- **Position**: 200 shares 长飞光纤 (601869), cost basis varies per entry
- **Cash**: ~11,638 yuan (insufficient for 1 lot 正T at 464+)
- **Stock price**: ~464 yuan/lot (high ATR: 6-8%, very volatile)
- **Strategy**: 反T only (先卖后买). 正T blocked by cash constraint.
- **Market regime**: Strong bull trend → 反T熔断 activates frequently (correct behavior)
- **Best environment** for this strategy: sideways or bear markets (反T thrives on pullbacks)

## Working Conventions

- All new QMT strategies go in `MyPy-Q/`, named `QMT_*.py`
- Use `_log()` wrapper (defined in each strategy) instead of raw `print()` — it prepends `[HH:MM:SS]`
- All QMT-injected functions (`get_trade_detail_data`, `order_shares`, `passorder`, callbacks) are NOT defined in `.py` files; IDE warnings about them are expected and should be ignored
- Strategy parameters are hardcoded as module-level constants (not JSON/YAML configs)
- Account ID is stored as `ACCOUNT = '8890145315'` in each strategy file

















<!-- cloude-code-toolbox:mcp-skills-awareness-begin -->

### MCP & Skills awareness (Cloude Code ToolBox)

_Last synced: 2026-08-20T01:18:51.909Z._

- **Full report:** `.Codex/cloude-code-toolbox-mcp-skills-awareness.md` in this workspace (auto-overwritten on each scan). Use it as ground truth for configured servers and skill folders.
- **MCP:** For **live tools** in Codex, enable the matching server via `/mcp`. Servers are configured in `~/.Codex.json` (user) and `.mcp.json` (project).
- **When the user’s task matches a server** (e.g. Confluence work and a **Confluence** / **Atlassian** MCP is listed), **prefer that server id** and plan on tool use—not only file search.
- **Skills:** Folders below contain `SKILL.md`; attach or cite paths in chat when relevant.

#### Workspace MCP

- `d:\02Project\QMT-export\.mcp.json` _(workspace: QMT-export)_ — _file missing_

_No active workspace servers in mcp.json._

#### User MCP

- `C:\Users\pp313\.Codex.json` — _servers defined_

| Server id | Kind | Detail |
|-----------|------|--------|
| pptmasterdeck-mcp | http | https://pptmasterdeck.clauxel.com/mcp |
| drawio | stdio | cmd /c npx -y drawio-mcp-server --editor |
| ppt-mcp | stdio | uvx ppt-mcp |
| pptogo-mcp | http | https://pptogo.com/api/mcp |

#### Project skills

- **git-commit** — `d:\02Project\QMT-export\.Codex\skills\git-commit` — Execute git commit with conventional commit message analysis, intelligent staging, and message generation. Use when user asks to commit changes, create a git commit, or mentions "/commit".

- **run-qmt-export** — `d:\02Project\QMT-export\.Codex\skills\run-qmt-export` — Build, run, smoke-test, and drive the QMT-export quantitative trading backtest system. Use when asked to run a backtest, verify the engine works, validate a strategy, test data loading, or check that the backtest system 

#### User skills

- **a-stock-data** — `C:\Users\pp313\.Codex\skills\a-stock-data` — A股全栈数据工具包 — 覆盖行情(mootdx+腾讯+百度K线)、研报(东财+同花顺+iwencai)、信号(同花顺热点+北向+龙虎榜+解禁+行业)、资金面(融资融券+大宗交易+股东户数+分红+资金流分钟级+资金流120日)、新闻(东财个股+全球资讯)、基础数据(mootdx财务/F10+东财+新浪三表)、公告(巨潮)七层数据源，内嵌全部调用代码，自包含零依赖外部文件。优先用通达信(mootdx)/腾讯(不封IP)，东财接口已内置限流防


- **QMT-expert** — `C:\Users\pp313\.Codex\skills\QMT-expert` — >

- **serenity-skill** — `C:\Users\pp313\.Codex\skills\serenity-skill` — Turn an investment agent into a supply-chain bottleneck hunter. Use this skill for source-backed investment research, live market/theme scans, AI/semi/technology value-chain mapping, A-share/HK/US stock screening, thesis

- **ask-matt** — `C:\Users\pp313\.agents\skills\ask-matt` — Ask which skill or flow fits your situation. A router over the skills in this repo.

- **Codex-handoff** — `C:\Users\pp313\.agents\skills\Codex-handoff` — Hand the current conversation off to a fresh background agent that picks up the work immediately.

- **code-review** — `C:\Users\pp313\.agents\skills\code-review` — Review the changes since a fixed point (commit, branch, tag, or merge-base) along two axes — Standards (does the code follow this repo's documented coding standards?) and Spec (does the code match what the originating is

- **codebase-design** — `C:\Users\pp313\.agents\skills\codebase-design` — Shared vocabulary for designing deep modules. Use when the user wants to design or improve a module's interface, find deepening opportunities, decide where a seam goes, make code more testable or AI-navigable, or when an

- **design-an-interface** — `C:\Users\pp313\.agents\skills\design-an-interface` — Generate multiple radically different interface designs for a module using parallel sub-agents. Use when user wants to design an API, explore interface options, compare module shapes, or mentions "design it twice".

- **diagnosing-bugs** — `C:\Users\pp313\.agents\skills\diagnosing-bugs` — Diagnosis loop for hard bugs and performance regressions. Use when the user says "diagnose"/"debug this", or reports something broken/throwing/failing/slow.

- **domain-modeling** — `C:\Users\pp313\.agents\skills\domain-modeling` — Build and sharpen a project's domain model. Use when the user wants to pin down domain terminology or a ubiquitous language, record an architectural decision, or when another skill needs to maintain the domain model.

- **edit-article** — `C:\Users\pp313\.agents\skills\edit-article` — Edit and improve articles by restructuring sections, improving clarity, and tightening prose. Use when user wants to edit, revise, or improve an article draft.

- **find-skills** — `C:\Users\pp313\.agents\skills\find-skills` — Helps users discover and install agent skills when they ask questions like "how do I do X", "find a skill for X", "is there a skill that can...", or express interest in extending capabilities. This skill should be used w

- **git-guardrails-Codex** — `C:\Users\pp313\.agents\skills\git-guardrails-Codex` — Set up Codex hooks to block dangerous git commands (push, reset --hard, clean, branch -D, etc.) before they execute. Use when user wants to prevent destructive git operations, add git safety hooks, or block git pus

- **grill-me** — `C:\Users\pp313\.agents\skills\grill-me` — A relentless interview to sharpen a plan or design.

- **grill-with-docs** — `C:\Users\pp313\.agents\skills\grill-with-docs` — A relentless interview to sharpen a plan or design, which also creates docs (ADR's and glossary) as we go.

- **grilling** — `C:\Users\pp313\.agents\skills\grilling` — Grill the user relentlessly about a plan or design. Use when the user wants to stress-test a plan before building, or uses any 'grill' trigger phrases.

- **handoff** — `C:\Users\pp313\.agents\skills\handoff` — Compact the current conversation into a handoff document for another agent to pick up.

- **implement** — `C:\Users\pp313\.agents\skills\implement` — Implement a piece of work based on a PRD or set of issues.

- **improve-codebase-architecture** — `C:\Users\pp313\.agents\skills\improve-codebase-architecture` — Scan a codebase for deepening opportunities, present them as a visual HTML report, then grill through whichever one you pick.

- **loop-me** — `C:\Users\pp313\.agents\skills\loop-me` — Grill me about specs for the workflows I want to build, within this workspace.

- **migrate-to-shoehorn** — `C:\Users\pp313\.agents\skills\migrate-to-shoehorn` — Migrate test files from `as` type assertions to @total-typescript/shoehorn. Use when user mentions shoehorn, wants to replace `as` in tests, or needs partial test data.

- **obsidian-vault** — `C:\Users\pp313\.agents\skills\obsidian-vault` — Search, create, and manage notes in the Obsidian vault with wikilinks and index notes. Use when user wants to find, create, or organize notes in Obsidian.

- **prototype** — `C:\Users\pp313\.agents\skills\prototype` — Build a throwaway prototype to answer a design question. Use when the user wants to sanity-check whether a state model or logic feels right, or explore what a UI should look like.

- **qa** — `C:\Users\pp313\.agents\skills\qa` — Interactive QA session where user reports bugs or issues conversationally, and the agent files GitHub issues. Explores the codebase in the background for context and domain language. Use when user wants to report bugs, d

- **request-refactor-plan** — `C:\Users\pp313\.agents\skills\request-refactor-plan` — Create a detailed refactor plan with tiny commits via user interview, then file it as a GitHub issue. Use when user wants to plan a refactor, create a refactoring RFC, or break a refactor into safe incremental steps.

- **research** — `C:\Users\pp313\.agents\skills\research` — Investigate a question against high-trust primary sources and capture the findings as a Markdown file in the repo. Use when the user wants a topic researched, docs or API facts gathered, or reading legwork delegated to a

- **resolving-merge-conflicts** — `C:\Users\pp313\.agents\skills\resolving-merge-conflicts` — Use when you need to resolve an in-progress git merge/rebase conflict.

- **scaffold-exercises** — `C:\Users\pp313\.agents\skills\scaffold-exercises` — Create exercise directory structures with sections, problems, solutions, and explainers that pass linting. Use when user wants to scaffold exercises, create exercise stubs, or set up a new course section.

- **setup-matt-pocock-skills** — `C:\Users\pp313\.agents\skills\setup-matt-pocock-skills` — Configure this repo for the engineering skills — set up its issue tracker, triage label vocabulary, and domain doc layout. Run once before first use of the other engineering skills.

- **setup-pre-commit** — `C:\Users\pp313\.agents\skills\setup-pre-commit` — Set up Husky pre-commit hooks with lint-staged (Prettier), type checking, and tests in the current repo. Use when user wants to add pre-commit hooks, set up Husky, configure lint-staged, or add commit-time formatting/typ

- **tdd** — `C:\Users\pp313\.agents\skills\tdd` — Test-driven development. Use when the user wants to build features or fix bugs test-first, mentions "red-green-refactor", or wants integration tests.

- **teach** — `C:\Users\pp313\.agents\skills\teach` — Teach the user a new skill or concept, within this workspace.

- **to-issues** — `C:\Users\pp313\.agents\skills\to-issues` — Break a plan, spec, or PRD into independently-grabbable issues on the project issue tracker using tracer-bullet vertical slices.

- **to-prd** — `C:\Users\pp313\.agents\skills\to-prd` — Turn the current conversation into a PRD and publish it to the project issue tracker — no interview, just synthesis of what you've already discussed.

- **triage** — `C:\Users\pp313\.agents\skills\triage` — Move issues and external PRs through a state machine of triage roles — categorise, verify, grill if needed, and write agent-ready briefs.

- **ubiquitous-language** — `C:\Users\pp313\.agents\skills\ubiquitous-language` — Extract a DDD-style ubiquitous language glossary from the current conversation, flagging ambiguities and proposing canonical terms. Saves to UBIQUITOUS_LANGUAGE.md. Use when user wants to define domain terms, build a glo

- **wayfinder** — `C:\Users\pp313\.agents\skills\wayfinder` — Plan a huge chunk of work — more than one agent session can hold — as a shared map of investigation tickets on your issue tracker, and resolve them one at a time until the way to the destination is clear.

- **wizard** — `C:\Users\pp313\.agents\skills\wizard` — Generate an interactive bash wizard that walks a human through a manual procedure — third-party setup, a one-off migration, an A→B state transition — opening URLs, capturing values, confirming each step, and writing .env

- **writing-beats** — `C:\Users\pp313\.agents\skills\writing-beats` — Writing, exploit — assemble raw material into a journey of beats, grounding each term before a beat leans on it.

- **writing-fragments** — `C:\Users\pp313\.agents\skills\writing-fragments` — Writing, explore — mine raw fragments, no structure yet.

- **writing-great-skills** — `C:\Users\pp313\.agents\skills\writing-great-skills` — Reference for writing and editing skills well — the vocabulary and principles that make a skill predictable.

- **writing-shape** — `C:\Users\pp313\.agents\skills\writing-shape` — Writing, exploit — shape raw material into an article, paragraph by paragraph.

<!-- cloude-code-toolbox:mcp-skills-awareness-end -->
