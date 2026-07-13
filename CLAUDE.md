# MI — Market Intelligence 自动化系统

这是 `~/MI` 的项目记忆文件。在这个目录下启动的 Claude Code 会话会自动读取它。

## 当前系统状态（2026-06-12）

**已从 Hermes 容器完全迁出，宿主机原生运行。** 2026-05-04 验证通过。

**邮件系统**：发信 Resend API，收信 Stalwart JMAP，永久有效 key，无 OAuth 刷新问题。

**情报质量重构已完成（2026-05-24）**：days=8 时效窗口、多格式日期解析、中文新闻补充（Serper News）、Jina 全文提取、V4 Pro 合成 + reasoning_effort=high、跨周去重（上周报告节注入 prefilter）、新 prompt（只写新增内容）。已在 6 家公司完整运行验证。

**KG 写入/检索机制已移除（2026-06-12）**：bridge 上 `/mempalace/kg/add_triple`、`/mempalace/kg/query` 两个端点已下线。`kg_extractor.py` 已删除，`run_intel.py` 不再调用三元组提取；`memory_context.py` 不再查询 KG 关系（仅保留 MemPalace 语义搜索 + Obsidian 全文搜索两路）。`~/.hermes/kg_vocab/` 词表未启用，未创建。

---

## 项目文档

| 文件 | 用途 | 操作规则 |
|------|------|---------|
| `~/MI/PITFALLS.md` | 详细踩坑记录（25 条，报错原文/修复代码/教训），从本文件拆出以控制体积 | 每次修复新故障后追加新条目到文件末尾；排查 bug 前先读 |
| `Hermes/MI/Hermes_MI设计文档.md`（Obsidian） | 系统架构设计文档，面向独立实现者，描述当前状态 | 架构/配置/模型选型变更时同步更新；"更新文档"指令必写 |
| `Hermes/MI/Hermes_MI开发日志.md`（Obsidian） | 开发决策与踩坑记录，条目从新到旧 | 每次重要变更后追加新条目；"更新文档"指令必写 |

新 session 启动时如涉及代码或架构讨论，应读取设计文档作为背景；涉及 bug 排查或改动前想确认"这里以前踩过坑没有"，读 `PITFALLS.md`。"更新文档"时三个文件均须更新（`PITFALLS.md` 只在有新踩坑时追加）。

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
├── .venv/                # uv sync 生成，Python 3.11
├── *.py                  # 所有脚本在根目录
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
| `com.hermes.emailcheck` | 每 5 分钟 | `~/MI/email_check.py` | `~/MI/logs/emailcheck.log` | `~/MI/logs/emailcheck-launchd.log` |
| `com.hermes.mi-slack-check` | 每 5 分钟 | `~/MI/slack_check.py` | `~/MI/logs/slack-check.log` | `~/MI/logs/slack-check-launchd.log` |
| `com.mi.sama-relay` | 每小时 | `~/MI/sama_relay.py` | `~/MI/logs/sama_relay.log` | `~/MI/logs/sama-relay-launchd.log` |

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
| `OPENROUTER_API_KEY` | LLM 调用（DeepSeek V4 Flash） |
| `TAVILY_API_KEY` | 情报抓取（主力，10次/日） |
| `SERPAPI_API_KEY` | Tavily 配额耗尽后备用（250次/月） |
| `SERPER_API_KEY` | 三级 fallback |
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
| `run_intel.py` | 情报主逻辑：搜索 → 去重 → LLM 分析 → Obsidian + 邮件 + Slack |
| `email_check.py` | 邮件轮询：JMAP 拉取 → 解析指令 → 三段式 followup pipeline |
| `email_sender.py` | 邮件发送（Resend API），返回 Resend email ID |
| `slack_sender.py` | Slack 发送（Bot Token 直连）：md_to_slack + 分块 thread |
| `slack_check.py` | Slack 轮询（5min）：thread 回复 bot 消息 或 `mi: ` 前缀 → followup pipeline |
| `sama_relay.py` | 每小时把 `PhysicalClue611/PC611-homepage` 仓库里标题含 `[sama]` 的最早一条 open issue 转发成邮件发给 `sama@openai.org`，成功后 close；运行开始时先核查上一次发送是否退信，退信则 reopen 重试 |
| `gmail_client.py` | **已弃用**，Gmail OAuth2 封装，保留备查 |
| `search_utils.py` | 三级搜索 fallback：Tavily → SerpApi → Serper |
| `config_store.py` | 公司/收件人配置，优先读 Obsidian watchlist.md |
| `dedup_utils.py` | L2 Jaccard + L3 MemPalace 去重 |
| `article_cache.py` | 文章全文缓存（90天TTL） |
| `http_utils.py` | httpx 重试封装（网络错误/5xx 重试，4xx 不重试） |
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
| 情报主合成 | `deepseek-v4-pro` + `reasoning_effort=high`（DeepSeek 直连） |
| Prefilter 门控 | `deepseek-v4-flash`（DeepSeek 直连） |
| 邮件指令解析 / 英文名推断 | `openai/gpt-oss-20b`（OpenRouter） |
| 情报主搜索 | Tavily `topic=general, days=8` → SerpApi → Serper |
| 中文新闻补充 | Serper News (`/news, hl=zh-cn`) → SerpApi News (`tbm=nws`) |

**OpenRouter 调用归属**（2026-07-12，`HTTP-Referer` 格式于 2026-07-13 修正，见 PITFALLS.md #25）：所有 `openrouter.ai/api/v1/chat/completions` 调用统一附加 `OR_ATTRIBUTION_HEADERS`（`HTTP-Referer: https://github.com/PhysicalClue611/China_Market_Intelligence`、`X-OpenRouter-Title: MI`），定义在各文件 `OPENROUTER_API_KEY` 常量旁，调用点用 `**OR_ATTRIBUTION_HEADERS` 合并进 headers，不手写字面量。`HTTP-Referer` 必须是合法 URL（`https://` 开头），裸字符串会被 OR 静默丢弃整个归属。调用点：`email_check.py`（3 处）、`run_intel_deepseek_test.py`（1 处）。全局约定见 `~/.claude/CLAUDE.md` "OpenRouter API 归属 Header 约定"；新增 OpenRouter 调用点时同样遵循。

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

详细踩坑记录（25 条，含具体报错、修复代码、教训）已拆到 **`PITFALLS.md`**（同目录）。排查 bug、判断某类故障是否已知、或改动前想确认"这里以前踩过坑没有"时读取该文件。本文件只保留最近几条的一句话索引，完整上下文一律看 `PITFALLS.md`：

- #21 JMAP 24 小时滑动窗口 → 长中断后指令永久丢失（已修复，issue #5）
- #22 JMAP 配置缺失非 fail-fast（已修复，issue #6）
- #23 Slack 游标在处理消息前就写入 + 不分页 → 崩溃/积压均永久丢消息（已修复，issue #7）
- #24 无日期转载文章绕过时效过滤 → 一年前旧财报被写成"本周新动态"（已修复，issue #12）
- #25 OpenRouter `HTTP-Referer` 用裸字符串非 URL → 归属 header 静默失效（已修复 2026-07-13）

---

## 去重架构

| 层 | 机制 | 状态 |
|---|---|---|
| 日期过滤 | `_parse_pub_date()`，多格式，> 9天丢弃 | 运行中 |
| L1 URL | `seen_urls.json` 90天TTL | 运行中 |
| L2 标题 Jaccard | `article_cache.json`，阈值 0.45 | 运行中 |
| L2.5 V4 Flash | 时效/相关性/跨周去重/信息量/事件日期抽取，max_tokens=1024 | 运行中 |
| 事件日期硬过滤 | `EVENT_MAX_AGE_DAYS=30`，对 V4 Flash 抽取的 event_date 做确定性丢弃，不信任模型自己的 keep 判断 | 运行中 |
| L3 MemPalace 语义 | 待积累 3 月以上数据后启用 | 未启用 |
