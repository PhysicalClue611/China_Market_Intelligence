# MI — Market Intelligence 自动化系统（Codex 入口）

本文件是 Codex 会话的入口，只放指针。项目规则只在一处维护，避免两份文件各自过时。

1. 项目规则、调度、环境变量、API 路由、踩坑索引：读 `CLAUDE.md`（权威来源，Claude Code 与 Codex 共用）。
2. **改代码、改配置、改 launchd 之前，必须先完整读 `docs/design.md`**（Obsidian 设计文档的逐字副本，不直接改它）。
3. 排查 bug、确认"以前踩过坑没有"读 `PITFALLS.md`。
4. GitHub 操作用项目 `.env` 的 `GITHUB_TOKEN` 走 REST API，不用 `gh` CLI（本机 `gh` 登录的是另一个账号，会用错身份）。
5. 2026-10-21 之前的 AGENTS.md（内容停在 2026-05，已过时）存档在 `docs/playbooks/agents_md_legacy_20261021.md`，只供溯源。
