# MI — Market Intelligence 自动化系统

这是 `~/MI` 的项目记忆文件。在这个目录下启动的 Claude Code 会话会自动读取它。

## 当前系统状态（2026-10-23）

**已从 Hermes 容器完全迁出，宿主机原生运行。** 2026-05-04 验证通过。

**邮件系统**：发信 Resend API，收信 Stalwart JMAP，永久有效 key，无 OAuth 刷新问题。

**周报管线已整体改造（2026-10-23，issue #23/#25，PR #24 合并为 `c7d95f7`）**：采集 = 东方财富公告 + Exa（每家 4 次：白名单媒体、开放、数字化运营、战略竞争；每行业 1 次），Google News/Tavily/Serper/Jina/DeepSeek/Gemma 已不在周报路径上；清洗 → Haiku 5.5 + GPT-6 Luna 两段式分类 → 材料只收公告与正文报道 → Haiku 起草、Luna 核查、Haiku 修订（均走 OpenRouter）。每家留档 `logs/audit-*.md`，每次运行 intel.log 一行 `COST`。规则在 `config/intel_rules.yaml`，集团映射 `config/entity_map.yaml`（草稿待确认）。样文实测 6 家 $0.53、13 分钟。**负责人决定先运行一段时间再看是否继续优化**；已知小问题（篇幅超 500 字、修订句出现"材料未显示"措辞）记在设计文档"已知问题与待观察"。信源评估与未测候选见 `docs/playbooks/intel_source_evaluation.md`。

**KG 写入/检索机制已移除（2026-06-12）**：bridge 上 `/mempalace/kg/add_triple`、`/mempalace/kg/query` 两个端点已下线。`kg_extractor.py` 已删除，`run_intel.py` 不再调用三元组提取；`memory_context.py` 不再查询 KG 关系（仅保留 MemPalace 语义搜索 + Obsidian 全文搜索两路）。`~/.hermes/kg_vocab/` 词表未启用，未创建。

**周任务 EINTR 防护（2026-09-01，issue #15）**：iCloud vault `glob` 遇 `InterruptedError` 短重试；公司循环隔离瞬时 OS 错误；全家失败且无 sections 则非零退出，不发「无新情报」。见 PITFALLS.md #30。


---

## 项目文档

| 文件 | 用途 | 操作规则 |
|------|------|---------|
| `docs/design.md` | 设计文档，Obsidian `Hermes_MI设计文档.md` 去掉 frontmatter 的逐字副本（agent 读这份） | 不直接改；改 Obsidian 源头后整体同步 |
| `~/MI/PITFALLS.md` | 详细踩坑记录（34 条，报错原文/修复代码/教训），从本文件拆出以控制体积 | 每次修复新故障后追加新条目到文件末尾；排查 bug 前先读 |
| `Hermes/MI/Hermes_MI设计文档.md`（Obsidian） | 设计文档源头，面向独立实现者，描述当前状态 | 架构/配置/模型选型变更时同步更新；"更新文档"指令必写 |
| `Hermes/MI/Hermes_MI开发日志.md`（Obsidian） | 开发决策与踩坑记录，早的在前、晚的在后 | 每次重要变更后 append 到文件末尾；"更新文档"指令必写 |
| `docs/playbooks/intel_source_evaluation.md` | 周报信源评估（2026-10-23）：实测结论、未测候选、重新评估触发条件 | 再评估信源时追加 |
| `docs/playbooks/agents_md_legacy_20261021.md` | 2026-10-21 之前的 AGENTS.md（内容停在 2026-05，已过时） | 只供溯源 |

**读取规则**：启动时只靠本文件，纯问答、杂项任务不读设计文档和开发日志。
**Why**：开发日志 88KB 是历史叙事，有效结论已提炼进本文件和 `PITFALLS.md`；设计文档只在要动系统时才需要。
- **改代码、改配置、改 launchd 之前，必须先完整读 `docs/design.md`**（2026-07-06 `.env` 缺 `FROM_ADDRESS`，邮件静默发不出去，而正确值就写在设计文档里）。本文件与它不一致时以后者为准。
- 排查 bug、确认"以前踩过坑没有"读 `PITFALLS.md`；开发日志只按 issue 编号/关键词检索，要近况只读最后 1–2 条。
- "更新文档"/"打扫战场"改了 Obsidian 设计文档，就同步 `docs/design.md`：从 vault 取原文，去 frontmatter，保留文件头关系说明，原子写入后逐行比对，随本次改动一起 commit。shell 读 iCloud 挂起约 1 分钟时，先提醒用户可能有授权弹窗。
- 仓库公开：设计文档源头不写真实收件人邮箱、密钥值等私人信息。

---

## 项目概述

每周自动拉取中国上市公司情报，写入 Obsidian vault 并推送邮件 / Telegram。
完全在宿主机运行，无需 Docker 容器介入。

---

## 目录结构

```
~/MI/
├── pyproject.toml        # 依赖声明（uv 管理）
├── uv.lock               # 锁定依赖版本
├── .env                  # API keys + HERMES_DATA/OBSIDIAN_PATH
├── .python-version       # uv 固定 3.14
├── .venv/                # uv sync 生成，Python 3.14（Homebrew python@3.14）
├── *.py                  # 所有脚本在根目录
├── config/               # intel_rules.yaml（周报规则）、entity_map.yaml（集团映射）
├── logs/                 # intel.log、audit-*.md（30 天清理）
└── data/
    ├── credentials.json  # Gmail OAuth client secret（已弃用，保留备查）
    ├── token.json        # Gmail OAuth token（已弃用，保留备查）
    ├── article_cache.json
    ├── seen_urls.json
    ├── fetch_log.json
    ├── processed_email_ids.json
    ├── jmap_cursor.json  # JMAP 轮询游标（最新 receivedAt），见 PITFALLS.md #21
    └── intel_config.yaml # 公司列表 fallback（主配置在 Obsidian watchlist.md）
```

---

## 调度（宿主机 launchd）

| plist | 周期 | 脚本 | 日志（Python logging，30天滚动） | launchd 原始 stdout/stderr（异常兜底） |
|---|---|---|---|---|
| `com.hermes.intel` | 周日 08:59 PDT | `~/MI/run_intel.py` | `~/MI/logs/intel.log` | `~/MI/logs/intel-launchd.log` |
| `com.hermes.emailcheck`（已停用 2026-10-08：无人使用；未来需要时改用非轮询方式，不再恢复轮询。plist 移至 `~/Library/LaunchAgents/.disabled/`） | 每 5 分钟 | `~/MI/email_check.py` | `~/MI/logs/emailcheck.log` | `~/MI/logs/emailcheck-launchd.log` |
| `com.hermes.mi-slack-check`（已停用 2026-10-08：无人使用；未来需要时改用非轮询方式，不再恢复轮询。plist 移至 `~/Library/LaunchAgents/.disabled/`） | 每 5 分钟 | `~/MI/slack_check.py` | `~/MI/logs/slack-check.log` | `~/MI/logs/slack-check-launchd.log` |
| `com.mi.sama-relay`（已停用，plist 移至 `~/Library/LaunchAgents/.disabled/`） | 每小时 | `~/MI/sama_relay.py` | `~/MI/logs/sama_relay.log` | `~/MI/logs/sama-relay-launchd.log` |

日志已从 `/tmp` 迁移到项目自身目录 `~/MI/logs/`（2026-07-11，见 issue #1 [Comment] / #2）：`/tmp` 下超过 3 天未访问的文件会被 macOS 每日 `periodic` 清理任务删除，导致周执行任务的日志"看似不存在"、巡检误报。新方案由 `log_utils.py` 的 `setup_logging()` 提供 `TimedRotatingFileHandler`（`backupCount=30`），三个脚本的 `if __name__ == "__main__":` 均已改为调用它；plist 的 `StandardOutPath`/`StandardErrorPath` 只作为 import 期崩溃等无法走 Python logging 的场景的兜底，正常运行不会写入。

launchd 直接调 `~/MI/.venv/bin/python`，无 Docker，无 LLM 介入。
`com.hermes.mempalace-bridge` 常驻运行（port 8765），为脚本提供 MemPalace/Obsidian API。

**手动触发：**
```bash
~/MI/.venv/bin/python ~/MI/run_intel.py
~/MI/.venv/bin/python ~/MI/run_intel.py --force   # 绕过去重
```

---

## 环境变量（~/MI/.env）

| 变量 | 用途 |
|---|---|
| `OPENROUTER_API_KEY` | 周报分类/起草/核查/修订；邮件指令 gpt-oss-20b |
| `EXA_API_KEY` | 周报 Exa 采集（必需） |
| `PARALLEL_API_KEY` | 2026-10-23 评估后未接入，仅保留 |
| `TAVILY_API_KEY` / `SERPAPI_API_KEY` / `SERPER_API_KEY` | 只剩邮件追问用，周报不用 |
| `TELEGRAM_BOT_TOKEN` | 推送通知 |
| `RESEND_API_KEY` | 邮件发送（Resend API，Sending access，`re_...`） |
| `RESEND_STATUS_KEY` | 邮件送达/退信状态查询（Resend API，Full access，`re_...`，与发信 key 分离，2026-07-12 新增） |
| `STALWART_API_KEY` | 邮件收信 JMAP 认证（`API_...`） |
| `JMAP_BASE` | Stalwart JMAP 服务器地址（`https://oci.physicalclue.us:8443`） |
| `JMAP_ACCOUNT_ID` | JMAP 账号 ID（`c`，见 PITFALLS.md #20） |
| `JMAP_INBOX_ID` | INBOX mailbox ID（`a`，见 PITFALLS.md #20） |
| `HERMES_DATA` | `~/MI/data`（脚本数据目录） |
| `OBSIDIAN_PATH` | Obsidian vault 绝对路径 |
| `SLACK_BOT_TOKEN` | Slack Bot Token（与 Hermes 共用，`xoxb-...`） |
| `SLACK_MI_CHANNEL` | Slack 投递频道 ID |
| `SLACK_ALLOWED_USERS` | 允许发 MI 追问的 Slack user ID（逗号分隔） |

---

## 脚本说明

| 脚本 | 用途 |
|---|---|
| `run_intel.py` | 情报主循环：采集 → 清洗 → 分类 → 合成 → Obsidian + 邮件 + Slack；`--force`/`--sample` |
| `intel_sources.py` / `intel_clean.py` / `intel_synth.py` | 采集（东方财富 + Exa）/ 清洗与两段式分类 / 组材料与起草-核查-修订 |
| `email_check.py` | 邮件轮询：JMAP 拉取 → 解析指令 → 三段式 followup pipeline |
| `email_sender.py` | 邮件发送（Resend API），返回 Resend email ID |
| `slack_sender.py` | Slack 发送（Bot Token 直连）：md_to_slack + 分块 thread |
| `slack_check.py` | Slack 轮询（5min）：thread 回复 bot 消息 或 `mi: ` 前缀 → followup pipeline |
| `sama_relay.py` | 每小时把 `PhysicalClue611/PC611-homepage` 仓库里标题含 `[sama]` 的最早一条 open issue 转发成邮件发给 `sama@openai.org`，成功后 close；运行开始时先核查上一次发送是否退信，退信则 reopen 重试 |
| `gmail_client.py` | **已弃用**，Gmail OAuth2 封装，保留备查 |
| `search_utils.py` | 追问用三级搜索 fallback：Tavily → SerpApi → Serper（周报不用） |
| `config_store.py` | 公司/收件人配置，优先读 Obsidian watchlist.md |
| `dedup_utils.py` / `article_cache.py` | 旧管线去重与缓存，周报已不调用 |
| `manual_send_test_email.py` | 手动发测试邮件（原 `test_email.py`，见 PITFALLS #33） |
| `http_utils.py` | httpx 重试封装（网络错误/5xx 重试，4xx 不重试）；`extract_llm_text()` 统一取 DeepSeek/推理模型 content/reasoning_content/reasoning；`call_llm_json()` 统一封装"调用+取值+解析 LLM JSON 输出"，内置对坏输出（非网络问题）的重试与诊断日志，见 PITFALLS.md #29 |
| `memory_context.py` | 每公司 LLM 前注入历史上下文（bridge REST）：MemPalace 语义搜索（三路 query）+ Obsidian 全文搜索 |

---

## 邮件架构

```
发信：email_sender.py → Resend API → smtp.resend.com → 目标 MX
      from: FROM_ADDRESS (env), reply_to: FROM_ADDRESS (env)

收信：外部回信 → Stalwart → MI 收件箱
      email_check.py → JMAP API (JMAP_BASE env) → 指令解析
```

**JMAP 常量（均从环境变量读取）：**
- `JMAP_BASE` = env var（Stalwart JMAP 服务器地址）
- `JMAP_ACCOUNT_ID` = env var（JMAP 账号 ID）
- `JMAP_INBOX_ID` = env var（INBOX mailbox ID）

**认证：** `STALWART_API_KEY` 直接作为 Bearer token，无需 PKCE OAuth 流程。

---

## API 路由策略

| 场景 | 模型 |
|---|---|
| 分类 | `anthropic/claude-haiku-5.5` 初筛 + `openai/gpt-6-luna` 复核，取严（OpenRouter，effort low） |
| 周报起草 / 修订 | `anthropic/claude-haiku-5.5`（OpenRouter，medium / low） |
| 事实核查 | `openai/gpt-6-luna`（OpenRouter，medium） |
| Followup 推理 | `deepseek-v4-pro`（DeepSeek 直连） |
| 邮件指令解析 / 英文名推断 | `openai/gpt-oss-20b`（OpenRouter） |
| 周报采集 | 东方财富公告 + Exa（不带 category；白名单 `includeDomains` / 其余 `excludeDomains`；20 条、正文 2000 字） |
| 追问搜索 | Tavily `topic=general, days=8` → SerpApi → Serper |

**OpenRouter 调用归属**（2026-07-12，`HTTP-Referer` 格式于 2026-07-13 修正，见 PITFALLS.md #25）：所有 `openrouter.ai/api/v1/chat/completions` 调用统一附加 `OR_ATTRIBUTION_HEADERS`（`HTTP-Referer: https://github.com/PhysicalClue611/China_Market_Intelligence`、`X-OpenRouter-Title: MI`），定义在各文件 `OPENROUTER_API_KEY` 常量旁，调用点用 `**OR_ATTRIBUTION_HEADERS` 合并进 headers，不手写字面量。`HTTP-Referer` 必须是合法 URL（`https://` 开头），裸字符串会被 OR 静默丢弃整个归属。调用点：`email_check.py`（3 处）、`intel_clean.py`（分类，常量定义在此）、`intel_synth.py`（起草/核查/修订，复用 `intel_clean` 的常量）、`run_intel_deepseek_test.py`（1 处）。全局约定见 `~/.claude/CLAUDE.md` "OpenRouter API 归属 Header 约定"；新增 OpenRouter 调用点时同样遵循。

**要求 JSON 输出的调用一律加 `response_format: {"type": "json_object"}`**（2026-07-19，见 PITFALLS.md #29）：DeepSeek 直连和 OpenRouter（含 `openai/gpt-oss-20b` 走 5-provider 池）均已用真实请求验证支持，返回 200 且输出为干净 JSON。从 API 层面约束输出格式，比事后解析更治本；新增任何"prompt 要求模型只输出 JSON"的调用点都应带上这个参数，不要只依赖 prompt 里的文字约束。取值/解析统一走 `http_utils.call_llm_json()`（内置重试+诊断日志，见上表 `http_utils.py` 行），不要在调用点各自手写。

---

## 依赖管理

```bash
# 重建 venv
cd ~/MI && uv sync

# 添加新包
cd ~/MI && uv add <package>

# 更新锁文件
cd ~/MI && uv lock --upgrade
```

---

## Bridge API（port 8765，宿主机常驻）

脚本通过 `http://localhost:8765` 访问。

| 端点 | 用途 |
|---|---|
| `POST /mempalace/search` | 语义搜索 |
| `POST /obsidian/search` | Obsidian 全文搜索 |
| `POST /obsidian/read` | 读取笔记 |

---

## 与 Hermes 的边界

MI 脚本对 Hermes Agent 完全无依赖：
- 不使用 Hermes 容器
- 不通过 Hermes cron 触发（`~/.hermes/cron/jobs.json` jobs 数组为空）
- 共用 bridge port 8765 和 Telegram bot（不同 bot token）

Obsidian 输出：`Paperview/Hermes/MI/YYYY-MM-DD-china-companies.md`

---

## 踩过的坑

详细踩坑记录（34 条，含具体报错、修复代码、教训）已拆到 **`PITFALLS.md`**（同目录）。排查 bug、判断某类故障是否已知、或改动前想确认"这里以前踩过坑没有"时读取该文件。本文件只保留最近几条的一句话索引，完整上下文一律看 `PITFALLS.md`：

- #24 无日期转载文章绕过时效过滤 → 一年前旧财报被写成"本周新动态"（已修复，issue #12）
- #25 OpenRouter `HTTP-Referer` 用裸字符串非 URL → 归属 header 静默失效（已修复 2026-07-13）
- #26 `_lookup_english_name` `max_tokens=30` 太小 → 推理耗尽预算，`content=null` 崩溃（已修复 2026-07-13）
- #27 Slack/email 先标记已处理后发送回复 + run_intel/slack_check 缺关键 env 仍 exit 0（已修复 2026-07-15，issue #8/#9/#11）
- #28 `prefilter_articles()` 漏 `reasoning_content` 兜底 → content=null 时门控静默 pass-through（已修复 2026-07-15，issue #10）
- #29 徐工年份错位事故：`json.loads` 无法区分"真空"与"格式错误"+ 手写重试补丁在 3 处调用点各自漏了 try/except 保护 → 引入 4 个新 bug，最终收敛为共享 `call_llm_json()` + `response_format=json_object`（已修复 2026-07-19）
- #30 iCloud scandir EINTR 打断周任务 → 整周报告静默缺失；glob 重试 + 公司循环隔离 + 全家失败 raise（已修复 2026-09-01，issue #15）
- #31 Flash 做结构化门控未关 thinking → 李宁 2 篇被原样回显成 JSON 数组、整层门控 pass-through；prefilter 换 OR `google/gemma-4-31b-it` + reasoning off（已修复 2026-09-01，issue #17）
- #32 换 venv 解释器后每个 LaunchAgent 首次启动被 macOS Launch Constraint 杀掉（-9、无日志）；周任务须在下次触发前 `launchctl kickstart` 一次让约束更新（2026-10-07，3.11 → 3.14 升级）
- #33 pytest 收集根目录 `test_email.py` 时真实给全部收件人发信 → 改名 `manual_send_test_email.py`；根目录 `test_*.py` 不得有模块级副作用（2026-10-23）
- #34 Google News 只有标题的条目被写成确定事实、核查没拦住 → 去掉 Google News，材料只收正文（2026-10-23，issue #25）

---

## 去重架构

| 层 | 机制 |
|---|---|
| 时间窗 | `window_days=8`，无日期丢弃；Exa 请求带 `startPublishedDate` |
| URL | `seen_urls.json` 90 天 TTL |
| 本周聚类 | 标题二元组 Jaccard ≥ 0.6，每簇留一条（公告 > 正文 > 标题） |
| 跨周 | 上周本节注入合成 prompt |
