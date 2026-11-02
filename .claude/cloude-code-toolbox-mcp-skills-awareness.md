# Claude Code ToolBox — MCP & Skills awareness

_Generated: 2026-10-15T01:23:17.131Z_

## How to use this report

- **Saved copy:** This file is **`.claude/cloude-code-toolbox-mcp-skills-awareness.md`** — refreshed whenever the toolbox runs an MCP & Skills scan (including on workspace open when auto-scan is enabled). It is meant for **Claude Code workspace context** together with `CLAUDE.md` (which gets a shorter replaceable summary when auto-merge is on).
- **MCP:** Lists **configured** servers from Claude Code config (`~/.claude.json` for user scope, `.mcp.json` for project scope). Use `/mcp` in the Claude Code panel to connect servers for your session.
- **Skills:** **On-disk** folders with `SKILL.md`. Claude Code does not auto-load them; attach `SKILL.md` or paths in chat when useful.
- **Task routing:** When the user’s request matches a server’s purpose (e.g. Confluence → Confluence/Atlassian MCP), prefer that **server id** from the tables below.

---

## MCP — workspace

Workspace `mcp.json` _(folder: QMT-expert)_

- **c:\03MyProj\QMT-expert\.mcp.json** — _File missing_

_No active workspace servers in mcp.json._

## MCP — user profile

- **C:\Users\pp313\.claude.json** — _File exists — servers defined_

| Server id | Kind | Detail |
|-----------|------|--------|
| pptmasterdeck-mcp | http | https://pptmasterdeck.clauxel.com/mcp |
| ppt-mcp | stdio | uvx ppt-mcp |
| pptogo-mcp | http | https://pptogo.com/api/mcp |
| drawio | stdio | cmd /c npx -y drawio-mcp-server --editor |

## Skills (local `SKILL.md` folders)

### Project-scoped

- **git-commit** — `c:\03MyProj\QMT-expert\.claude\skills\git-commit`
  - Execute git commit with conventional commit message analysis, intelligent staging, and message generation. Use when user asks to commit changes, create a git commit, or mentions "/commit".

- **QMT-expert** — `c:\03MyProj\QMT-expert\.claude\skills\QMT-expert`
  - >

- **run-qmt-expert** — `c:\03MyProj\QMT-expert\.claude\skills\run-qmt-expert`
  - Build, run, smoke-test, and drive the QMT-expert quantitative trading backtest system. Use when asked to run a backtest, verify the engine works, validate a strategy, test data loading, or check that the backtest system 

- **signal-validation** — `c:\03MyProj\QMT-expert\.claude\skills\signal-validation`
  - Validate A-stock sell/top-detection signals against historical data. Run when asked to validate trading signals, backtest sell rules, check momentum indicators, evaluate escape-top signals, or verify technical indicators

- **git-commit** — `c:\03MyProj\QMT-expert\.agents\skills\git-commit`
  - Execute git commit with conventional commit message analysis, intelligent staging, and message generation. Use when user asks to commit changes, create a git commit, or mentions "/commit".

- **QMT-expert** — `c:\03MyProj\QMT-expert\.agents\skills\QMT-expert`
  - >

- **qmt-safe-commit** — `c:\03MyProj\QMT-expert\.agents\skills\qmt-safe-commit`
  - Safely review, verify, stage, and commit changes in the QMT-expert repository. Use when asked to commit QMT strategies, MiniQMT infrastructure, backtest code, tests, analysis, or documentation. Do not use for stash creat

- **signal-validation** — `c:\03MyProj\QMT-expert\.agents\skills\signal-validation`
  - Validate A-stock sell/top-detection signals against historical data. Run when asked to validate trading signals, backtest sell rules, check momentum indicators, evaluate escape-top signals, or verify technical indicators

### User-scoped

- **a-stock-data** — `C:\Users\pp313\.claude\skills\a-stock-data`
  - A股全栈数据工具包 — 覆盖行情(mootdx+腾讯+百度K线)、研报(东财+同花顺+iwencai)、信号(同花顺热点+北向+龙虎榜+解禁+行业)、资金面(融资融券+大宗交易+股东户数+分红+资金流分钟级+资金流120日)、新闻(东财个股+全球资讯)、基础数据(mootdx财务/F10+东财+新浪三表)、公告(巨潮)七层数据源，内嵌全部调用代码，自包含零依赖外部文件。优先用通达信(mootdx)/腾讯(不封IP)，东财接口已内置限流防

- **A15-expert** — `C:\Users\pp313\.claude\skills\A15-expert`
  - >

- **autosar-bsw-expert** — `C:\Users\pp313\.claude\skills\autosar-bsw-expert`
  - >

- **CCU-expert** — `C:\Users\pp313\.claude\skills\CCU-expert`
  - >

- **check-expert** — `C:\Users\pp313\.claude\skills\check-expert`
  - 我将为你提供一套 **核对模块** 的功能性核对点。请你扮演系统核查专家，针对**每一个核对点**，严格按照预设的专家角色、输出规范和目录结构，生成3份相互关联的Markdown报告。

- **diag-expert** — `C:\Users\pp313\.claude\skills\diag-expert`
  - >

- **QMT-expert** — `C:\Users\pp313\.claude\skills\QMT-expert`
  - >

- **rollout-check** — `C:\Users\pp313\.claude\skills\rollout-check`
  - >

- **serenity-skill** — `C:\Users\pp313\.claude\skills\serenity-skill`
  - Turn an investment agent into a supply-chain bottleneck hunter. Use this skill for source-backed investment research, live market/theme scans, AI/semi/technology value-chain mapping, A-share/HK/US stock screening, thesis

- **solution-verify** — `C:\Users\pp313\.claude\skills\solution-verify`
  - >

- **tricore-expert** — `C:\Users\pp313\.claude\skills\tricore-expert`
  - >

- **a-stock-data** — `C:\Users\pp313\.agents\skills\a-stock-data`
  - A股全栈数据工具包 — 覆盖行情(mootdx+腾讯+百度K线)、研报(东财+同花顺+iwencai)、信号(同花顺热点+北向+龙虎榜+解禁+行业)、资金面(融资融券+大宗交易+股东户数+分红+资金流分钟级+资金流120日)、新闻(东财个股+全球资讯)、基础数据(mootdx财务/F10+东财+新浪三表)、公告(巨潮)七层数据源，内嵌全部调用代码，自包含零依赖外部文件。优先用通达信(mootdx)/腾讯(不封IP)，东财接口已内置限流防

- **A15-expert** — `C:\Users\pp313\.agents\skills\A15-expert`
  - >

- **ask-matt** — `C:\Users\pp313\.agents\skills\ask-matt`
  - Ask which skill or flow fits your situation. A router over the skills in this repo.

- **autosar-bsw-expert** — `C:\Users\pp313\.agents\skills\autosar-bsw-expert`
  - >

- **CCU-expert** — `C:\Users\pp313\.agents\skills\CCU-expert`
  - >

- **check-expert** — `C:\Users\pp313\.agents\skills\check-expert`
  - 我将为你提供一套 **核对模块** 的功能性核对点。请你扮演系统核查专家，针对**每一个核对点**，严格按照预设的专家角色、输出规范和目录结构，生成3份相互关联的Markdown报告。

- **claude-handoff** — `C:\Users\pp313\.agents\skills\claude-handoff`
  - Hand the current conversation off to a fresh background agent that picks up the work immediately.

- **code-review** — `C:\Users\pp313\.agents\skills\code-review`
  - Review the changes since a fixed point (commit, branch, tag, or merge-base) along two axes — Standards (does the code follow this repo's documented coding standards?) and Spec (does the code match what the originating is

- **codebase-design** — `C:\Users\pp313\.agents\skills\codebase-design`
  - Shared vocabulary for designing deep modules. Use when the user wants to design or improve a module's interface, find deepening opportunities, decide where a seam goes, make code more testable or AI-navigable, or when an

- **design-an-interface** — `C:\Users\pp313\.agents\skills\design-an-interface`
  - Generate multiple radically different interface designs for a module using parallel sub-agents. Use when user wants to design an API, explore interface options, compare module shapes, or mentions "design it twice".

- **diag-expert** — `C:\Users\pp313\.agents\skills\diag-expert`
  - >

- **diagnosing-bugs** — `C:\Users\pp313\.agents\skills\diagnosing-bugs`
  - Diagnosis loop for hard bugs and performance regressions. Use when the user says "diagnose"/"debug this", or reports something broken/throwing/failing/slow.

- **domain-modeling** — `C:\Users\pp313\.agents\skills\domain-modeling`
  - Build and sharpen a project's domain model. Use when the user wants to pin down domain terminology or a ubiquitous language, record an architectural decision, or when another skill needs to maintain the domain model.

- **edit-article** — `C:\Users\pp313\.agents\skills\edit-article`
  - Edit and improve articles by restructuring sections, improving clarity, and tightening prose. Use when user wants to edit, revise, or improve an article draft.

- **find-skills** — `C:\Users\pp313\.agents\skills\find-skills`
  - Helps users discover and install agent skills when they ask questions like "how do I do X", "find a skill for X", "is there a skill that can...", or express interest in extending capabilities. This skill should be used w

- **git-guardrails-claude-code** — `C:\Users\pp313\.agents\skills\git-guardrails-claude-code`
  - Set up Claude Code hooks to block dangerous git commands (push, reset --hard, clean, branch -D, etc.) before they execute. Use when user wants to prevent destructive git operations, add git safety hooks, or block git pus

- **grill-me** — `C:\Users\pp313\.agents\skills\grill-me`
  - A relentless interview to sharpen a plan or design.

- **grill-with-docs** — `C:\Users\pp313\.agents\skills\grill-with-docs`
  - A relentless interview to sharpen a plan or design, which also creates docs (ADR's and glossary) as we go.

- **grilling** — `C:\Users\pp313\.agents\skills\grilling`
  - Grill the user relentlessly about a plan or design. Use when the user wants to stress-test a plan before building, or uses any 'grill' trigger phrases.

- **handoff** — `C:\Users\pp313\.agents\skills\handoff`
  - Compact the current conversation into a handoff document for another agent to pick up.

- **implement** — `C:\Users\pp313\.agents\skills\implement`
  - Implement a piece of work based on a PRD or set of issues.

- **improve-codebase-architecture** — `C:\Users\pp313\.agents\skills\improve-codebase-architecture`
  - Scan a codebase for deepening opportunities, present them as a visual HTML report, then grill through whichever one you pick.

- **loop-me** — `C:\Users\pp313\.agents\skills\loop-me`
  - Grill me about specs for the workflows I want to build, within this workspace.

- **migrate-to-shoehorn** — `C:\Users\pp313\.agents\skills\migrate-to-shoehorn`
  - Migrate test files from `as` type assertions to @total-typescript/shoehorn. Use when user mentions shoehorn, wants to replace `as` in tests, or needs partial test data.

- **obsidian-vault** — `C:\Users\pp313\.agents\skills\obsidian-vault`
  - Search, create, and manage notes in the Obsidian vault with wikilinks and index notes. Use when user wants to find, create, or organize notes in Obsidian.

- **prototype** — `C:\Users\pp313\.agents\skills\prototype`
  - Build a throwaway prototype to answer a design question. Use when the user wants to sanity-check whether a state model or logic feels right, or explore what a UI should look like.

- **qa** — `C:\Users\pp313\.agents\skills\qa`
  - Interactive QA session where user reports bugs or issues conversationally, and the agent files GitHub issues. Explores the codebase in the background for context and domain language. Use when user wants to report bugs, d

- **QMT-expert** — `C:\Users\pp313\.agents\skills\QMT-expert`
  - >

- **request-refactor-plan** — `C:\Users\pp313\.agents\skills\request-refactor-plan`
  - Create a detailed refactor plan with tiny commits via user interview, then file it as a GitHub issue. Use when user wants to plan a refactor, create a refactoring RFC, or break a refactor into safe incremental steps.

- **research** — `C:\Users\pp313\.agents\skills\research`
  - Investigate a question against high-trust primary sources and capture the findings as a Markdown file in the repo. Use when the user wants a topic researched, docs or API facts gathered, or reading legwork delegated to a

- **resolving-merge-conflicts** — `C:\Users\pp313\.agents\skills\resolving-merge-conflicts`
  - Use when you need to resolve an in-progress git merge/rebase conflict.

- **rollout-check** — `C:\Users\pp313\.agents\skills\rollout-check`
  - >

- **scaffold-exercises** — `C:\Users\pp313\.agents\skills\scaffold-exercises`
  - Create exercise directory structures with sections, problems, solutions, and explainers that pass linting. Use when user wants to scaffold exercises, create exercise stubs, or set up a new course section.

- **serenity-skill** — `C:\Users\pp313\.agents\skills\serenity-skill`
  - Turn an investment agent into a supply-chain bottleneck hunter. Use this skill for source-backed investment research, live market/theme scans, AI/semi/technology value-chain mapping, A-share/HK/US stock screening, thesis

- **setup-matt-pocock-skills** — `C:\Users\pp313\.agents\skills\setup-matt-pocock-skills`
  - Configure this repo for the engineering skills — set up its issue tracker, triage label vocabulary, and domain doc layout. Run once before first use of the other engineering skills.

- **setup-pre-commit** — `C:\Users\pp313\.agents\skills\setup-pre-commit`
  - Set up Husky pre-commit hooks with lint-staged (Prettier), type checking, and tests in the current repo. Use when user wants to add pre-commit hooks, set up Husky, configure lint-staged, or add commit-time formatting/typ

- **solution-verify** — `C:\Users\pp313\.agents\skills\solution-verify`
  - >

- **tdd** — `C:\Users\pp313\.agents\skills\tdd`
  - Test-driven development. Use when the user wants to build features or fix bugs test-first, mentions "red-green-refactor", or wants integration tests.

- **teach** — `C:\Users\pp313\.agents\skills\teach`
  - Teach the user a new skill or concept, within this workspace.

- **to-issues** — `C:\Users\pp313\.agents\skills\to-issues`
  - Break a plan, spec, or PRD into independently-grabbable issues on the project issue tracker using tracer-bullet vertical slices.

- **to-prd** — `C:\Users\pp313\.agents\skills\to-prd`
  - Turn the current conversation into a PRD and publish it to the project issue tracker — no interview, just synthesis of what you've already discussed.

- **triage** — `C:\Users\pp313\.agents\skills\triage`
  - Move issues and external PRs through a state machine of triage roles — categorise, verify, grill if needed, and write agent-ready briefs.

- **tricore-expert** — `C:\Users\pp313\.agents\skills\tricore-expert`
  - >

- **ubiquitous-language** — `C:\Users\pp313\.agents\skills\ubiquitous-language`
  - Extract a DDD-style ubiquitous language glossary from the current conversation, flagging ambiguities and proposing canonical terms. Saves to UBIQUITOUS_LANGUAGE.md. Use when user wants to define domain terms, build a glo

- **wayfinder** — `C:\Users\pp313\.agents\skills\wayfinder`
  - Plan a huge chunk of work — more than one agent session can hold — as a shared map of investigation tickets on your issue tracker, and resolve them one at a time until the way to the destination is clear.

- **wizard** — `C:\Users\pp313\.agents\skills\wizard`
  - Generate an interactive bash wizard that walks a human through a manual procedure — third-party setup, a one-off migration, an A→B state transition — opening URLs, capturing values, confirming each step, and writing .env

- **writing-beats** — `C:\Users\pp313\.agents\skills\writing-beats`
  - Writing, exploit — assemble raw material into a journey of beats, grounding each term before a beat leans on it.

- **writing-fragments** — `C:\Users\pp313\.agents\skills\writing-fragments`
  - Writing, explore — mine raw fragments, no structure yet.

- **writing-great-skills** — `C:\Users\pp313\.agents\skills\writing-great-skills`
  - Reference for writing and editing skills well — the vocabulary and principles that make a skill predictable.

- **writing-shape** — `C:\Users\pp313\.agents\skills\writing-shape`
  - Writing, exploit — shape raw material into an article, paragraph by paragraph.

---

## Suggested next steps

- **MCP:** Use this extension’s hub **MCP** tab, or `claude mcp list` in the terminal. In Claude Code, use `/mcp` to connect servers for the session.
- **Edit config:** Open `~/.claude.json` (user MCP) or `<workspace>/.mcp.json` (project MCP) via the extension commands.
- **Refresh this report:** run **Intelligence — scan MCP & Skills awareness** again after changing MCP config or adding skills.

_Report from Claude Code ToolBox extension._
