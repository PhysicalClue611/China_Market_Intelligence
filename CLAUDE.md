# MI — Market Intelligence 自动化系统

这是 `~/MI` 的项目记忆文件。在这个目录下启动的 Claude Code 会话会自动读取它。

## 当前系统状态（2026-06-12）

**已从 Hermes 容器完全迁出，宿主机原生运行。** 2026-05-04 验证通过。

**邮件系统**：发信 Resend API，收信 Stalwart JMAP，永久有效 key，无 OAuth 刷新问题。

**情报质量重构已完成（2026-05-24）**：days=8 时效窗口、多格式日期解析、中文新闻补充（Serper News）、Jina 全文提取、V4 Pro 合成 + reasoning_effort=high、跨周去重（上周报告节注入 prefilter）、新 prompt（只写新增内容）。已在 6 家公司完整运行验证。

**KG 写入/检索机制已移除（2026-06-12）**：bridge 上 `/mempalace/kg/add_triple`、`/mempalace/kg/query` 两个端点已下线。`kg_extractor.py` 已删除，`run_intel.py` 不再调用三元组提取；`memory_context.py` 不再查询 KG 关系（仅保留 MemPalace 语义搜索 + Obsidian 全文搜索两路）。`~/.hermes/kg_vocab/` 词表未启用，未创建。

---

## 项目文档（Obsidian）

| 文件 | 用途 | 操作规则 |
|------|------|---------|
| `Hermes/MI/Hermes_MI设计文档.md` | 系统架构设计文档，面向独立实现者，描述当前状态 | 架构/配置/模型选型变更时同步更新；"更新文档"指令必写 |
| `Hermes/MI/Hermes_MI开发日志.md` | 开发决策与踩坑记录，条目从新到旧 | 每次重要变更后追加新条目；"更新文档"指令必写 |

新 session 启动时如涉及代码或架构讨论，应读取设计文档作为背景。"更新文档"时两个文件均须更新。

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
    ├── jmap_cursor.json  # JMAP 轮询游标（最新 receivedAt），见踩坑 #21
    └── intel_config.yaml # 公司列表 fallback（主配置在 Obsidian watchlist.md）
```

---

## 调度（宿主机 launchd）

| plist | 周期 | 脚本 | 日志（Python logging，30天滚动） | launchd 原始 stdout/stderr（异常兜底） |
|---|---|---|---|---|
| `com.hermes.intel` | 周日 08:59 PDT | `~/MI/run_intel.py` | `~/MI/logs/intel.log` | `~/MI/logs/intel-launchd.log` |
| `com.hermes.emailcheck` | 每 5 分钟 | `~/MI/email_check.py` | `~/MI/logs/emailcheck.log` | `~/MI/logs/emailcheck-launchd.log` |
| `com.hermes.mi-slack-check` | 每 5 分钟 | `~/MI/slack_check.py` | `~/MI/logs/slack-check.log` | `~/MI/logs/slack-check-launchd.log` |

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
| `RESEND_API_KEY` | 邮件发送（Resend API，`re_...`） |
| `STALWART_API_KEY` | 邮件收信 JMAP 认证（`API_...`） |
| `JMAP_BASE` | Stalwart JMAP 服务器地址（`https://oci.physicalclue.us:8443`） |
| `JMAP_ACCOUNT_ID` | JMAP 账号 ID（`c`，见踩坑 #20） |
| `JMAP_INBOX_ID` | INBOX mailbox ID（`a`，见踩坑 #20） |
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
| 邮件指令解析 / 英文名推断 | `openai/gpt-oss-20b` |
| 情报主搜索 | Tavily `topic=general, days=8` → SerpApi → Serper |
| 中文新闻补充 | Serper News (`/news, hl=zh-cn`) → SerpApi News (`tbm=nws`) |

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

### 1. Prefilter max_tokens 过小导致 content=null（已修复 2026-05-24）
V4 Flash prefilter 原 `max_tokens=200`，推理模型在 reasoning 阶段就耗尽 token，`content` 返回 null，JSON decode 失败触发 pass-through 降级，导致跨周去重实际不执行。已改为 `max_tokens=1024`，并在 prompt 明确禁止 CoT 输出。

### 2. load_dotenv 路径层级
脚本从 `~/.hermes/skills/intel/` 搬至 `~/MI/` 后，`load_dotenv` 的相对路径少一级 `..`。已修复为 `os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")`。

### 3. bridge URL 在容器内外不同
容器内用 `host.lima.internal:8765`，宿主机原生运行用 `localhost:8765`。迁出时已全部改为 `localhost:8765`（`dedup_utils.py`、`memory_context.py`）。

### 4. L2 去重与当批文章互相比对（已修复 2026-05-04）
`fetch_company_raw` 在返回前将当批文章写入 `article_cache`，随后 `get_articles_by_company` 读出的 cache 已包含本批条目，导致同批次相似文章互相命中 L2 Jaccard，两篇都被丢弃。修复：在调 `fetch_company_raw` **之前**快照历史 cache，L2 对比只用历史快照。

### 5. 搜索失败被记录为"当日已抓取"（完整修复 2026-05-04）
上轮修复只改了 `fetch_company_raw` 的 `except` 分支，但 `search()` 全 provider 失败走 `return []` 正常返回，不抛异常，`except` 永远不触发。完整修复：`search_utils.py` 全 provider 失败改为 `raise RuntimeError`，`fetch_company_raw` 的 `except` 捕获后返回 `None`，主循环跳过 `fetch_log` 写入。

### 6. launchd 下 daemon thread 被进程退出杀死（已修复 2026-05-04）
`add_recipient` 和 `run_intel` 指令启动 `daemon=True` 线程，`run_email_check()` 返回后 launchd 进程立即退出，daemon thread 被 kill，情报拉取和完成邮件永远发不出去。修复：改为 `daemon=False`，收集到模块级列表，`run_email_check()` 末尾 join + clear。

### 7. watchlist.md section heading 被当注释跳过（已修复 2026-05-04）
`_parse_watchlist()` 先检查 `line.startswith("#")`，`## companies` / `## recipients` 被拦截跳过，section 永远不切换，整个 watchlist 解析为空，静默 fallback 到 YAML。修复：heading 匹配（`==` 精确比较）移到 `startswith("#")` 过滤之前。

### 8. `_write_watchlist()` 旧收件人泄漏到文件末尾（已修复 2026-05-04）
上轮 writer 修复在 `past_recipients = True` 后将旧收件人条目放入 `post`，写回时出现在新 recipients block 之后；parser 修复后会被重新解析，导致"删除收件人"不生效。修复：改用四状态机（pre→companies→recipients→post），recipients 状态下的旧条目直接跳过。

### 9. `_build_status_report()` 对 dict 列表调用 `join` 崩溃（已修复 2026-05-04）
`cfg["companies"]` 是 `[{"zh": ..., "en": ...}]`，直接 `", ".join(...)` 抛 `TypeError`，`run_email_check()` 崩溃，邮件不回复，消息 ID 不标记处理，launchd 下轮反复重试同一封信。修复：改为 generator 取 `c["zh"]`。

### 10. Tavily `topic=news` 对中文公司名查询失效
Tavily `topic="news"` 主要聚合英文新闻源，中文公司名 query 返回完全不相关的结果（查"海尔集团"返回惠而浦、LG、富士康）。`search_utils.py` 固定使用 `topic="general"` + 双语 query（`{en} {zh} latest news strategy financials`）。勿改回 `news`。

### 11. DeepSeek V4 Flash content 可能为 null（推理模型通用问题）
DeepSeek V4 Flash 是推理模型，先在 `reasoning` 字段生成思考链，最终答案写入 `content`。若 `max_tokens` 过小，token 在推理阶段耗尽，`content` 保持 `null`，直接取值会崩溃。
**所有调用点的取值方式：** `msg.get("content") or msg.get("reasoning") or ""`
**`max_tokens` 设置：** prefilter 512，synthesis 8000，followup 各阶段 2000+。

### 12. OpenRouter SSL EOF / RemoteProtocolError 导致单公司全轮失败
`httpx` 对 OpenRouter 的长时推理请求偶发 `RemoteProtocolError: Server disconnected without sending a response`（SSL EOF）。在 http_utils.py 引入之前，一次瞬态抖动会导致该公司所有 LLM 调用全部失败。`http_utils.py` 的 `post_with_retry` 处理：网络类错误 + 5xx 最多重试 2 次（2s/4s 退避），4xx（含 429）不重试，三次全失败返回 `(None, error_str)` 而非崩溃。所有外部 API 调用必须走此封装。

### 13. Stalwart JMAP messageId 不含尖括号（2026-05-07）
JMAP RFC 8621 规定 `messageId` 字段已剥除 `<>`，而 `In-Reply-To` header 需要 `<msgid@domain>` 格式。`email_sender.py` 中已加规范化：`mid = reply_to_msg_id if reply_to_msg_id.startswith("<") else f"<{reply_to_msg_id}>"`。

### 14. 返回类型从 str 改为 list 后，isinstance 守卫静默失效
函数返回类型变更时必须同步检查所有调用点的类型守卫。`send_report()` 返回类型从 `str` 改为 `list` 后，调用点的 `if sid and isinstance(sid, str): _save_processed_id(sid)` 对列表求值为 False，静默跳过保存——没有报错，但 dedup ID 实际未写入。

### 15. Telegram bot token 前缀 ≠ 用户 chat ID
Bot token 格式 `{bot_user_id}:{secret}`，冒号前是 bot 自身 Telegram user ID，不是用户的 chat ID。向用户发消息应使用用户的 personal user ID（`TELEGRAM_ALLOWED_USERS` 中的值），用 bot_user_id 作为 chat_id 发消息静默失败。

### 16. 新增搜索 provider 代码上线后必须确认 key 已进 .env
添加新 provider（如 Serper.dev）后，首次运行若仍显示 `SERPER_API_KEY not set`，原因是 key 仅存在于 shell 环境变量未写入 `~/MI/.env`。代码 ready ≠ provider 可用，两步独立：(1) 写入 .env，(2) 脚本读到后才生效。验证：运行后检查日志是否出现 `search via Serper`。

### 17. dry run 污染 article_cache 导致正式运行误报"无新情报"（2026-05-24）
直接调用 `fetch_company_raw()` 的 dry run 会写入 `article_cache.json`（`save_articles()` 无 force 判断）。多轮 dry run 后，本周文章全部进入 cache，正式运行时 L2 title dedup 全部命中，误报"无新情报"。

**正式重跑前的正确清除步骤（缺一不可）**：
```python
# 1. fetch_log: 清除今日条目（v != today）
# 2. seen_urls: 清除今日写入（ts < today_ts）
# 3. article_cache (flat dict {url: meta}): 清除今日写入（meta['ts'] < today_ts）
# 4. 删除 Obsidian 今日报告文件
```
注意 article_cache 是 flat dict（顶层 key 是 URL），不是按公司组织的嵌套结构，迭代时不能直接用 `.items()` 取公司 key。

### 18. OpenRouter NovitaAI 对结构化提取质量极差（2026-06-01 实测）
同一 model id 在不同 provider 上运行精度不同。NovitaAI 激进量化（Q3/Q4），结构化提取时实体长度超限、规则遵从性差、`described_as` 滥用、对称重复。`kg_extractor.py` 的 `OR_PROVIDER` 已设为 `["DigitalOcean", "DeepSeek", "Together", "Fireworks"]`。DigitalOcean（FP16/BF16）质量与精度相当于 R1/NovitaAI，速度快 10 倍，成本低一个数量级。**所有结构化提取任务均不得用 NovitaAI。**

### 19. `.env` 缺失 `FROM_ADDRESS` 导致 Resend 邮件静默 422（已修复 2026-07-06，issue #1）
`email_sender.py` 用 `os.getenv("FROM_ADDRESS", "")` 取值，`.env` 中若整行缺失（非置空）不会报错，拼出 `from: "Hermes MI <>"` 无效 header，Resend 返回 422。Telegram/Slack 通知仍会成功，掩盖邮件通道故障，仅在日志里留一行不含响应体的 ERROR，难以定位。**修复**：`.env` 补齐 `FROM_ADDRESS=MI@physicalclue.us`；`email_sender.py` 新增前置守卫（`FROM_ADDRESS` 为空直接拒绝发送 + ERROR 日志）+ `httpx.HTTPStatusError` 单独捕获并记录完整响应体。**教训**：新环境变量上线后（尤其是迁移场景，如本例 2026-05-07 Gmail→Resend 迁移），必须实际核实 `.env` 中确有其值，不能只看 `.env.example` 或代码默认值；第三方 API 调用的异常处理必须包含响应体，否则日志无法支撑事后排查。

### 20. `.env` 缺失 `JMAP_BASE`/`JMAP_ACCOUNT_ID`/`JMAP_INBOX_ID` 导致 email_check.py 静默故障 3 周以上（已修复 2026-07-11，issue #2）
`email_check.py` 用 `os.getenv("JMAP_BASE", "")` 兜底空字符串，拼出 `/jmap/` 无效 URL，每次调用报 `Request URL is missing an 'http://' or 'https://' protocol.`。异常被 catch 后只记 ERROR、进程正常退出（exit 0），launchd 判定"成功"，巡检脚本看不出异常——收信指令通道（加公司/加收件人/邮件触发 run_intel）静默失效至少从 2026-06-17 到 2026-07-11（`processed_email_ids.json` 期间无新写入）。**排查确认**：三个变量不是被 git 误删——`.env` 本就在 `.gitignore` 里、从未进过版本历史，纯粹是本地从未配全。`JMAP_BASE` 从 Obsidian 迁移文档（`Hermes/MI/邮件系统迁移说明.md`，Paperview 仓库 2026-06-29 提交）找回；`JMAP_ACCOUNT_ID`/`JMAP_INBOX_ID` 通过对生产 Stalwart 服务器发起只读 JMAP `session`/`Mailbox/get` 查询独立确认得到真实值 `c`/`a`，与文档记录吻合。**修复**：`.env` 补齐三个变量，端到端发送真实邮件验证收信链路打通。**教训**：捕获异常后只记 ERROR 不报警的静默降级，对每 5 分钟跑一次的高频任务是最危险的模式——launchd exit code 和巡检都看不出来，只能靠"功能是否真的在工作"这类主动验证发现；关键环境变量应在脚本启动时做存在性校验，缺失时 fail-fast，而不是构造出无效值继续空转。

### 21. JMAP 24 小时滑动窗口导致长时间中断后指令永久丢失（已修复 2026-07-12，issue #5）
`_jmap_fetch_inbox` 原实现每次只拉取"过去 24 小时"内的邮件，无游标、无补抓机制。若轮询本身中断超过 24 小时（如踩坑 #20 那次 3 周静默故障），恢复后早于滑动截止线的指令邮件对查询而言直接消失，`processed_email_ids.json` 只记录见过的 ID，无法枚举"从未见过"的历史邮件。**修复**：`email_check.py` 改为持久化游标（`data/jmap_cursor.json`，原子写入 tmp+`os.replace`），记录最新处理邮件的 `receivedAt`，每处理完一封（含未授权发件人跳过的情况）立即推进；读取时回退 10 分钟重叠窗口防时钟误差漏信，重复邮件靠既有 `processed_ids` 吸收；`_jmap_fetch_inbox` 改用升序 + `position` 分页取尽，不再受单次 50 条上限截断；新增 `--backfill-hours` CLI 参数，可在灾后手动指定有界范围补抓，成功后仍推进游标。首次部署时无游标文件，自动退化为原 24 小时窗口，行为不变。**教训**：滑动时间窗口类轮询若无持久游标，本质上是"只能容忍短暂中断"的设计，一旦中断时长超过窗口，故障期内的数据是真正丢失而非延迟——这类系统应默认假设中断可能超过窗口，游标持久化是标配而非可选加固。

---

## 去重架构

| 层 | 机制 | 状态 |
|---|---|---|
| 日期过滤 | `_parse_pub_date()`，多格式，> 9天丢弃 | 运行中 |
| L1 URL | `seen_urls.json` 90天TTL | 运行中 |
| L2 标题 Jaccard | `article_cache.json`，阈值 0.45 | 运行中 |
| L2.5 V4 Flash | 时效/相关性/跨周去重/信息量，max_tokens=1024 | 运行中 |
| L3 MemPalace 语义 | 待积累 3 月以上数据后启用 | 未启用 |
