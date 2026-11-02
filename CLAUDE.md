# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
关于策略的py文件如果有改动，则新建py文件
## Repository Purpose

A股量化交易策略开发与实盘运行环境。核心标的是**长飞光纤(601869)**，围绕日内做T、选股策略、回测系统三大方向展开。

## Key Rules via `/QMT-expert` Skill

When answering QMT API questions, the Skill at `C:\Users\pp313\.claude\skills\QMT-expert\SKILL.md` mandates:
- **Never invent** function names, parameters, or features — verify against `references/md/python/python_api.md` first
- **Cite sources** as `[Python API, p.XX]` or `[系统功能, p.XX]`
- **Answer completely in one reply** — don't pause mid-response to ask "should I continue?"
- **Code examples always** — full signatures + parameter docs + runnable code

QMT API reference is in `references/md/` (python, systemfunction, vba subdirectories).

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

File encoding must be `# -*- coding: gbk -*-` for Chinese QMT client.

## Current State (July 2026)

- **Position**: 200 shares 长飞光纤 (601869), cost basis varies per entry
- **Cash**: ~11,638 yuan (insufficient for 1 lot 正T at 464+)
- **Stock price**: ~464 yuan/lot (high ATR: 6-8%, very volatile)
- **Strategy**: 反T only (先卖后买). 正T blocked by cash constraint.
- **Market regime**: Strong bull trend → 反T熔断 activates frequently (correct behavior)
- **Best environment** for this strategy: sideways or bear markets (反T thrives on pullbacks)

## Working Conventions

- All new QMT strategies go in `MyPy-Q/`, named `QMT_*.py`
- When the user asks for strategy adjustments → **create a new file**, increment version number
- Use `_log()` wrapper (defined in each strategy) instead of raw `print()` — it prepends `[HH:MM:SS]`
- All QMT-injected functions (`get_trade_detail_data`, `order_shares`, `passorder`, callbacks) are NOT defined in `.py` files; IDE warnings about them are expected and should be ignored
- Strategy parameters are hardcoded as module-level constants (not JSON/YAML configs)
- Account ID is stored as `ACCOUNT = '8890145315'` in each strategy file

































<!-- cloude-code-toolbox:mcp-skills-awareness-begin -->

### MCP & Skills awareness (Claude Code ToolBox)

_Last synced: 2026-10-15T01:23:17.162Z._

- **Full report:** `.claude/cloude-code-toolbox-mcp-skills-awareness.md` in this workspace (auto-overwritten on each scan). Use it as ground truth for configured servers and skill folders.
- **MCP:** For **live tools** in Claude Code, enable the matching server via `/mcp`. Servers are configured in `~/.claude.json` (user) and `.mcp.json` (project).
- **When the user’s task matches a server** (e.g. Confluence work and a **Confluence** / **Atlassian** MCP is listed), **prefer that server id** and plan on tool use—not only file search.
- **Skills:** Folders below contain `SKILL.md`; attach or cite paths in chat when relevant.

#### Workspace MCP

- `c:\03MyProj\QMT-expert\.mcp.json` _(workspace: QMT-expert)_ — _file missing_

_No active workspace servers in mcp.json._

#### User MCP

- `C:\Users\pp313\.claude.json` — _servers defined_

| Server id | Kind | Detail |
|-----------|------|--------|
| pptmasterdeck-mcp | http | https://pptmasterdeck.clauxel.com/mcp |
| ppt-mcp | stdio | uvx ppt-mcp |
| pptogo-mcp | http | https://pptogo.com/api/mcp |
| drawio | stdio | cmd /c npx -y drawio-mcp-server --editor |

#### Project skills

- **git-commit** — `c:\03MyProj\QMT-expert\.claude\skills\git-commit` — Execute git commit with conventional commit message analysis, intelligent staging, and message generation. Use when user asks to commit changes, create a git commit, or mentions "/commit".

- **QMT-expert** — `c:\03MyProj\QMT-expert\.claude\skills\QMT-expert` — >

- **run-qmt-expert** — `c:\03MyProj\QMT-expert\.claude\skills\run-qmt-expert` — Build, run, smoke-test, and drive the QMT-expert quantitative trading backtest system. Use when asked to run a backtest, verify the engine works, validate a strategy, test data loading, or check that the backtest system 

- **signal-validation** — `c:\03MyProj\QMT-expert\.claude\skills\signal-validation` — Validate A-stock sell/top-detection signals against historical data. Run when asked to validate trading signals, backtest sell rules, check momentum indicators, evaluate escape-top signals, or verify technical indicators

- **git-commit** — `c:\03MyProj\QMT-expert\.agents\skills\git-commit` — Execute git commit with conventional commit message analysis, intelligent staging, and message generation. Use when user asks to commit changes, create a git commit, or mentions "/commit".

- **QMT-expert** — `c:\03MyProj\QMT-expert\.agents\skills\QMT-expert` — >

- **qmt-safe-commit** — `c:\03MyProj\QMT-expert\.agents\skills\qmt-safe-commit` — Safely review, verify, stage, and commit changes in the QMT-expert repository. Use when asked to commit QMT strategies, MiniQMT infrastructure, backtest code, tests, analysis, or documentation. Do not use for stash creat

- **signal-validation** — `c:\03MyProj\QMT-expert\.agents\skills\signal-validation` — Validate A-stock sell/top-detection signals against historical data. Run when asked to validate trading signals, backtest sell rules, check momentum indicators, evaluate escape-top signals, or verify technical indicators

#### User skills

- **a-stock-data** — `C:\Users\pp313\.claude\skills\a-stock-data` — A股全栈数据工具包 — 覆盖行情(mootdx+腾讯+百度K线)、研报(东财+同花顺+iwencai)、信号(同花顺热点+北向+龙虎榜+解禁+行业)、资金面(融资融券+大宗交易+股东户数+分红+资金流分钟级+资金流120日)、新闻(东财个股+全球资讯)、基础数据(mootdx财务/F10+东财+新浪三表)、公告(巨潮)七层数据源，内嵌全部调用代码，自包含零依赖外部文件。优先用通达信(mootdx)/腾讯(不封IP)，东财接口已内置限流防

- **A15-expert** — `C:\Users\pp313\.claude\skills\A15-expert` — >

- **autosar-bsw-expert** — `C:\Users\pp313\.claude\skills\autosar-bsw-expert` — >

- **CCU-expert** — `C:\Users\pp313\.claude\skills\CCU-expert` — >

- **check-expert** — `C:\Users\pp313\.claude\skills\check-expert` — 我将为你提供一套 **核对模块** 的功能性核对点。请你扮演系统核查专家，针对**每一个核对点**，严格按照预设的专家角色、输出规范和目录结构，生成3份相互关联的Markdown报告。

- **diag-expert** — `C:\Users\pp313\.claude\skills\diag-expert` — >

- **QMT-expert** — `C:\Users\pp313\.claude\skills\QMT-expert` — >

- **rollout-check** — `C:\Users\pp313\.claude\skills\rollout-check` — >

- **serenity-skill** — `C:\Users\pp313\.claude\skills\serenity-skill` — Turn an investment agent into a supply-chain bottleneck hunter. Use this skill for source-backed investment research, live market/theme scans, AI/semi/technology value-chain mapping, A-share/HK/US stock screening, thesis

- **solution-verify** — `C:\Users\pp313\.claude\skills\solution-verify` — >

- **tricore-expert** — `C:\Users\pp313\.claude\skills\tricore-expert` — >

- **a-stock-data** — `C:\Users\pp313\.agents\skills\a-stock-data` — A股全栈数据工具包 — 覆盖行情(mootdx+腾讯+百度K线)、研报(东财+同花顺+iwencai)、信号(同花顺热点+北向+龙虎榜+解禁+行业)、资金面(融资融券+大宗交易+股东户数+分红+资金流分钟级+资金流120日)、新闻(东财个股+全球资讯)、基础数据(mootdx财务/F10+东财+新浪三表)、公告(巨潮)七层数据源，内嵌全部调用代码，自包含零依赖外部文件。优先用通达信(mootdx)/腾讯(不封IP)，东财接口已内置限流防

- **A15-expert** — `C:\Users\pp313\.agents\skills\A15-expert` — >

- **ask-matt** — `C:\Users\pp313\.agents\skills\ask-matt` — Ask which skill or flow fits your situation. A router over the skills in this repo.

- **autosar-bsw-expert** — `C:\Users\pp313\.agents\skills\autosar-bsw-expert` — >

- **CCU-expert** — `C:\Users\pp313\.agents\skills\CCU-expert` — >

- **check-expert** — `C:\Users\pp313\.agents\skills\check-expert` — 我将为你提供一套 **核对模块** 的功能性核对点。请你扮演系统核查专家，针对**每一个核对点**，严格按照预设的专家角色、输出规范和目录结构，生成3份相互关联的Markdown报告。

- **claude-handoff** — `C:\Users\pp313\.agents\skills\claude-handoff` — Hand the current conversation off to a fresh background agent that picks up the work immediately.

- **code-review** — `C:\Users\pp313\.agents\skills\code-review` — Review the changes since a fixed point (commit, branch, tag, or merge-base) along two axes — Standards (does the code follow this repo's documented coding standards?) and Spec (does the code match what the originating is

- **codebase-design** — `C:\Users\pp313\.agents\skills\codebase-design` — Shared vocabulary for designing deep modules. Use when the user wants to design or improve a module's interface, find deepening opportunities, decide where a seam goes, make code more testable or AI-navigable, or when an

- **design-an-interface** — `C:\Users\pp313\.agents\skills\design-an-interface` — Generate multiple radically different interface designs for a module using parallel sub-agents. Use when user wants to design an API, explore interface options, compare module shapes, or mentions "design it twice".

- **diag-expert** — `C:\Users\pp313\.agents\skills\diag-expert` — >

- **diagnosing-bugs** — `C:\Users\pp313\.agents\skills\diagnosing-bugs` — Diagnosis loop for hard bugs and performance regressions. Use when the user says "diagnose"/"debug this", or reports something broken/throwing/failing/slow.

- **domain-modeling** — `C:\Users\pp313\.agents\skills\domain-modeling` — Build and sharpen a project's domain model. Use when the user wants to pin down domain terminology or a ubiquitous language, record an architectural decision, or when another skill needs to maintain the domain model.

- **edit-article** — `C:\Users\pp313\.agents\skills\edit-article` — Edit and improve articles by restructuring sections, improving clarity, and tightening prose. Use when user wants to edit, revise, or improve an article draft.

- **find-skills** — `C:\Users\pp313\.agents\skills\find-skills` — Helps users discover and install agent skills when they ask questions like "how do I do X", "find a skill for X", "is there a skill that can...", or express interest in extending capabilities. This skill should be used w

- **git-guardrails-claude-code** — `C:\Users\pp313\.agents\skills\git-guardrails-claude-code` — Set up Claude Code hooks to block dangerous git commands (push, reset --hard, clean, branch -D, etc.) before they execute. Use when user wants to prevent destructive git operations, add git safety hooks, or block git pus

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

- **QMT-expert** — `C:\Users\pp313\.agents\skills\QMT-expert` — >

- **request-refactor-plan** — `C:\Users\pp313\.agents\skills\request-refactor-plan` — Create a detailed refactor plan with tiny commits via user interview, then file it as a GitHub issue. Use when user wants to plan a refactor, create a refactoring RFC, or break a refactor into safe incremental steps.

- **research** — `C:\Users\pp313\.agents\skills\research` — Investigate a question against high-trust primary sources and capture the findings as a Markdown file in the repo. Use when the user wants a topic researched, docs or API facts gathered, or reading legwork delegated to a

- **resolving-merge-conflicts** — `C:\Users\pp313\.agents\skills\resolving-merge-conflicts` — Use when you need to resolve an in-progress git merge/rebase conflict.

- **rollout-check** — `C:\Users\pp313\.agents\skills\rollout-check` — >

- **scaffold-exercises** — `C:\Users\pp313\.agents\skills\scaffold-exercises` — Create exercise directory structures with sections, problems, solutions, and explainers that pass linting. Use when user wants to scaffold exercises, create exercise stubs, or set up a new course section.

- **serenity-skill** — `C:\Users\pp313\.agents\skills\serenity-skill` — Turn an investment agent into a supply-chain bottleneck hunter. Use this skill for source-backed investment research, live market/theme scans, AI/semi/technology value-chain mapping, A-share/HK/US stock screening, thesis

- **setup-matt-pocock-skills** — `C:\Users\pp313\.agents\skills\setup-matt-pocock-skills` — Configure this repo for the engineering skills — set up its issue tracker, triage label vocabulary, and domain doc layout. Run once before first use of the other engineering skills.

- **setup-pre-commit** — `C:\Users\pp313\.agents\skills\setup-pre-commit` — Set up Husky pre-commit hooks with lint-staged (Prettier), type checking, and tests in the current repo. Use when user wants to add pre-commit hooks, set up Husky, configure lint-staged, or add commit-time formatting/typ

- **solution-verify** — `C:\Users\pp313\.agents\skills\solution-verify` — >

- **tdd** — `C:\Users\pp313\.agents\skills\tdd` — Test-driven development. Use when the user wants to build features or fix bugs test-first, mentions "red-green-refactor", or wants integration tests.

- **teach** — `C:\Users\pp313\.agents\skills\teach` — Teach the user a new skill or concept, within this workspace.

- **to-issues** — `C:\Users\pp313\.agents\skills\to-issues` — Break a plan, spec, or PRD into independently-grabbable issues on the project issue tracker using tracer-bullet vertical slices.

- **to-prd** — `C:\Users\pp313\.agents\skills\to-prd` — Turn the current conversation into a PRD and publish it to the project issue tracker — no interview, just synthesis of what you've already discussed.

- **triage** — `C:\Users\pp313\.agents\skills\triage` — Move issues and external PRs through a state machine of triage roles — categorise, verify, grill if needed, and write agent-ready briefs.

- **tricore-expert** — `C:\Users\pp313\.agents\skills\tricore-expert` — >

- **ubiquitous-language** — `C:\Users\pp313\.agents\skills\ubiquitous-language` — Extract a DDD-style ubiquitous language glossary from the current conversation, flagging ambiguities and proposing canonical terms. Saves to UBIQUITOUS_LANGUAGE.md. Use when user wants to define domain terms, build a glo

- **wayfinder** — `C:\Users\pp313\.agents\skills\wayfinder` — Plan a huge chunk of work — more than one agent session can hold — as a shared map of investigation tickets on your issue tracker, and resolve them one at a time until the way to the destination is clear.

- **wizard** — `C:\Users\pp313\.agents\skills\wizard` — Generate an interactive bash wizard that walks a human through a manual procedure — third-party setup, a one-off migration, an A→B state transition — opening URLs, capturing values, confirming each step, and writing .env

- **writing-beats** — `C:\Users\pp313\.agents\skills\writing-beats` — Writing, exploit — assemble raw material into a journey of beats, grounding each term before a beat leans on it.

- **writing-fragments** — `C:\Users\pp313\.agents\skills\writing-fragments` — Writing, explore — mine raw fragments, no structure yet.

- **writing-great-skills** — `C:\Users\pp313\.agents\skills\writing-great-skills` — Reference for writing and editing skills well — the vocabulary and principles that make a skill predictable.

- **writing-shape** — `C:\Users\pp313\.agents\skills\writing-shape` — Writing, exploit — shape raw material into an article, paragraph by paragraph.

<!-- cloude-code-toolbox:mcp-skills-awareness-end -->
