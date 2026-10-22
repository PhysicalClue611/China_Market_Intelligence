> **本文件与 Obsidian 版本的关系**：作者本人的版本维护在私有 Obsidian vault（`Hermes/MI/Hermes_MI设计文档.md`），本文件是去掉 frontmatter 后的逐字同步副本，供 agent 和其他实现者阅读。只改 Obsidian 源头，再整体同步到这里，不直接编辑本文件。按时间的变更记录在 Obsidian《Hermes_MI开发日志》，踩坑细节在仓库 `PITFALLS.md`。

# Hermes MI 设计文档

面向独立实现者。描述系统当前状态，不记录历史变更过程。

---

## 系统目标

每周自动拉取中国上市公司的商业情报，经 LLM 合成为 IT 咨询视角的分析报告，写入 Obsidian vault 并推送邮件和 Telegram。支持通过邮件回复动态调整监控公司和收件人列表，以及发起 ad-hoc 情报查询。

---

## 运行环境

- **宿主机**：Mac，`~/MI/`
- **Python**：3.14（Homebrew `python@3.14`），`~/MI/.venv/`（uv 管理）。换解释器后每个 LaunchAgent 首次启动会被 macOS Launch Constraint 杀一次，周任务须在下次触发前 `launchctl kickstart` 一次（PITFALLS.md #32）
- **调度**：Mac launchd（无 Docker 容器介入）
- **Bridge**：`~/.hermes/mempalace_bridge.py` 常驻，port 8765，宿主机直连

---

## 整体架构

```
[ launchd 周日 08:59 PDT ]
         |
    run_intel.py
         |
   search_utils.search() ──────────────────────► Tavily (days=8) / SerpApi / Serper  [主搜索]
   search_utils.search_cn() ───────────────────► Serper News / SerpApi News (CN)      [中文补充]
         |
   fetch_company_raw() ─────────────────────────► date filter (pub >9d) + relevance + staleness
         |
   article_cache.py + dedup_utils.py ─────────► article_cache.json / seen_urls.json (L1+L2)
         |
   load_last_report_section() ─────────────────► Obsidian 上周报告（跨周去重素材）
         |
   prefilter_articles() ──────────────────────► OpenRouter Gemma 31B IT, reasoning off (质量+时效+跨周去重)
         |
   memory_context.py ──────────────────────────► Bridge :8765 → MemPalace + Obsidian
         |
   synthesize_with_llm() ──────────────────────► DeepSeek V4 Pro + reasoning_effort=high
         |
   ┌─────────────────────────────────────────────────────────┐
   │  Obsidian vault / Hermes/MI/YYYY-MM-DD-china-companies  │
   └─────────────────────────────────────────────────────────┘
         |
   email_sender.py ────────────────────────────► Resend API → 收件人
         |
   _send_telegram_alert() ─────────────────────► Telegram Bot API
         |
   slack_sender.post_report() ─────────────────► Slack API → SLACK_MI_CHANNEL

[ launchd 每 5 分钟（已停用 2026-10-08，可手动运行） ]
         |
    slack_check.py ──────────────────────────── → 轮询 SLACK_MI_CHANNEL 追问（见下注）
         |
    email_check.py
         |
   _jmap_fetch_inbox() ────────────────────────► Stalwart JMAP (oci.physicalclue.us:8443)
         |
   _parse_command() ───────────────────────────► OpenRouter (gpt-oss-20b)
         |
   _execute_command()
    ├── config_store.py（增减公司/收件人）
    ├── _followup_three_stage()（分析提问）─────► OpenRouter gpt-oss-20b + DeepSeek V4 Pro 直连
    └── run_intel()（立即生成）─────────────────► 同上情报主循环
         |
   email_sender.py ────────────────────────────► Resend API → 发件人
```

---

## 数据流：情报主循环（run_intel.py）

1. **配置加载**：从 `watchlist.md`（优先）或 `intel_config.yaml`（fallback）读取公司列表和收件人
2. **频率控制**：`fetch_log.json` 记录每公司每日抓取状态，`--force` 绕过
3. **跨周上下文加载**：`load_last_report_section()` 从 Obsidian 最近历史报告提取该公司节，供 prefilter 和 synthesis 注入。iCloud vault 的 `glob` 遇 `InterruptedError`/`EINTR` 短重试，耗尽返回 `""`（本家不做跨周去重注入），不打死整轮。公司循环隔离瞬时 OS 错误；全家失败且无 sections 则非零退出，不发「无新情报」（issue #15，PITFALLS #30）
4. **主搜索**：Tavily（days=8）→ SerpApi → Serper fallback，全失败抛 `RuntimeError`，主循环 skip（不写 fetch_log）
5. **中文补充搜索**：`search_cn()` 调 Serper News → SerpApi News，失败返回 `[]`（非致命），按 URL 去重后 merge
6. **发布日期过滤（方案 B）**：解析 `published_date`（ISO / 中文绝对 / 中文相对 / 英文相对），> 9 天丢弃；无法解析 pass-through
7. **相关性过滤**：中文名前两字或英文名第一词命中即保留
8. **年份过滤（方案 A 补充）**：标题中 max year < current_year - 1 则丢弃（旧年报）
9. **Jina 全文提取**：对无 `published_date` 的文章（英文 Tavily 结果）调 `r.jina.ai` 取全文，替换短 snippet；有 `published_date` 的 Serper News 文章不处理（已知时效）。失败静默回退，上限 `JINA_ENRICH_MAX=5` 条/公司
10. **文章入缓存**：`article_cache.json`（90天TTL，flat dict `{url: meta}`），全文存储，在 Jina 丰富后、LLM 截断前写入
11. **L1 URL 去重**：`seen_urls.json`（90天TTL），已见 URL 跳过
12. **L2 标题去重**：Jaccard 相似度 ≥ 0.45 视为重复（年份 20xx 保留，其余数字过滤），比对对象为历史缓存快照
13. **Prefilter（L2.5）**：OpenRouter `google/gemma-4-31b-it`，`reasoning.enabled=false`，`max_tokens=4096`，`response_format=json_object`，`temperature=0`；provider `order=[Crusoe, Friendli, OpenInference]`，`ignore=[Together]`，`allow_fallbacks=false`。合并执行：时效性（含无日期文章主动判断）/ 相关性 / 跨周去重（注入上周报告节）/ 信息量门控；输出 length_hint（200/400/600）。返回值含显式 `status`（`ok`/`llm_failed_passthrough`），写入 FUNNEL 日志的 `prefilter_status`，区分“LLM 调用/解析失败被迫放行”与“模型真的判断全部保留”（2026-07-15，issue #10）。调用/解析统一走 `http_utils.call_llm_json()`（可选 `meta_cb` 把 OR provider 与 `reasoning_tokens` 写进 intel.log，形如 `[李宁] Prefilter LLM: provider=Crusoe, reasoning_tokens=0`，字段缺失记 `n/a`，回调异常只记 warning；2026-09-01，issue #17）。headers 带 `OR_ATTRIBUTION_HEADERS`。报告 footer 推理层写作「{LLM_MODEL_PRO} (synthesis, DeepSeek) / {PREFILTER_MODEL} (prefilter, OpenRouter)」
14. **知识库注入**：`memory_context.py` 调 bridge 拉 MemPalace 语义搜索 + Obsidian 全文搜索上下文
15. **LLM 合成**：DeepSeek V4 Pro，`reasoning_effort=high`，`max_tokens=8000`；prompt 注入上周报告节，要求只写新增内容，无新动态输出一句话后停止
16. **Obsidian 写入**：`{OBSIDIAN_PATH}/Hermes/MI/YYYY-MM-DD-china-companies.md`
17. **邮件发送**：Resend API，from=`MI@physicalclue.us`
18. **Telegram 通知**：有情报发摘要，无情报也发通知
19. **Slack 投递**：`slack_sender.post_report()` 发到 `SLACK_MI_CHANNEL`（mrkdwn，超长自动分块串 thread）

---

## 数据流：邮件指令循环（email_check.py）

> launchd 轮询（`com.hermes.emailcheck`，每 5 分钟）已于 2026-10-08 停用，plist 在 `~/Library/LaunchAgents/.disabled/`。以下是手动运行 `email_check.py` 时的行为。

1. **JMAP 拉取**：从持久化游标 `jmap_cursor.json`（上次成功处理的最新 `receivedAt`）回退 `CURSOR_OVERLAP_MINUTES=10` 分钟起，分页（每页 50 封）拉取 `MI@physicalclue.us` 收件箱；游标缺失/损坏时退化为过去 24 小时；`run_email_check(backfill_hours=N)` 按指定小时数回扫（issue #5）
2. **ID 去重**：`processed_email_ids.json`（72小时TTL），已处理 ID 跳过
3. **授权过滤**：发件人 email 不在收件人列表中则记录并跳过
4. **指令解析**：gpt-oss-20b 解析邮件意图，输出 action JSON
5. **指令执行**：
   - `add/remove_company`：修改 watchlist.md + intel_config.yaml
   - `add/remove_recipient`：修改配置；新增收件人时异步触发 run_intel（daemon=False 线程）
   - `status_report`：返回当前配置和调度信息
   - `run_intel`：异步触发情报主循环
   - `followup_question`：三段式流水线（见下）
6. **回复**：Resend API 发送带 `In-Reply-To` header 的回复邮件。**回复发送成功后才推进处理状态**（`_save_processed_id`/游标），发送失败则不推进，留给下一轮重试；同批次内一旦有失败，后续邮件即便成功也不再推进游标（`cursor_stalled`），防止跳过失败邮件（2026-07-15，issue #9）
7. **Telegram alert**：同步指令发完成通知，异步指令发"执行中"→"完成"
8. **等待后台线程**：`run_email_check()` 末尾 join + clear 所有后台线程

### Followup 三段式流水线

- **Stage 1**（gpt-oss-20b）：提取核心问题、实体、2条搜索 query、分析框架
- **Stage 2**（search_utils）：Tavily → SerpApi → Serper 检索，最多 2×5=10 条结果
- **Stage 3**（`deepseek-v4-pro`，DeepSeek 直连，`reasoning_effort=high`，`max_tokens=2000`）：基于情报和框架深度推理，约 400 字中文报告

---

## 脚本清单与职责

| 脚本 | 职责 |
|------|------|
| `run_intel.py` | 情报主循环入口，协调搜索/去重/LLM/输出；末尾并列投递邮件、Telegram、Slack |
| `email_check.py` | 邮件轮询、指令解析与执行（launchd 已停用 2026-10-08，可手动运行） |
| `email_sender.py` | Resend API 封装，Markdown → HTML |
| `slack_sender.py` | Slack 发送：md_to_slack 转换 + 超长分块串 thread；`post_report()` 读 `SLACK_MI_CHANNEL` |
| `slack_check.py` | Slack 轮询（launchd 每 5 分钟，已停用 2026-10-08）：thread 回复 bot 消息 或 `mi: ` 前缀 → followup pipeline |
| `search_utils.py` | 三级搜索 fallback（Tavily→SerpApi→Serper） |
| `config_store.py` | watchlist.md + intel_config.yaml 读写 |
| `dedup_utils.py` | L2 Jaccard 去重 + L3 MemPalace 语义去重 |
| `article_cache.py` | 文章全文缓存（90天TTL） |
| `http_utils.py` | httpx 重试封装（网络错误/5xx 重试，4xx 不重试）；`call_llm_json()` 统一封装"调用 LLM + 取值 + 解析 JSON"，仅对坏输出（非网络问题）重试，见下方"LLM JSON 输出解析"一节 |
| `memory_context.py` | 每公司 LLM 前注入历史上下文（bridge REST）：三路 MemPalace 语义搜索 + Obsidian 全文搜索 |
| `hermes_footer.py` | 邮件 footer（下次轮询时间提示） |
| `gmail_client.py` | **DEPRECATED** — Gmail OAuth2 封装，不再使用 |
| `test_dedup.py` | 去重系统离线校准工具 |
| `test_email.py` | 邮件系统手动测试工具 |
| `test_prefilter.py` / `test_eintr_retry.py` / `test_jmap_config.py` | 单测：prefilter 门控、iCloud EINTR 重试、JMAP 配置 fail-fast |
| `log_utils.py` | `setup_logging()`：`~/MI/logs/` 下 `TimedRotatingFileHandler`，30 天滚动 |
| `sama_relay.py` | 把 `PhysicalClue611/PC611-homepage` 中标题含 `[sama]` 的最早一条 open issue 转发成邮件，成功后 close；launchd `com.mi.sama-relay` 已停用 |
| `run_intel_deepseek_test.py` | 一次性测试脚本（V4 Flash 全量分析，发 `TEST_RECIPIENT`），非生产路径 |

---

## 去重架构

| 层 | 机制 | 状态 | 文件 |
|---|---|---|---|
| 日期过滤 | `_parse_pub_date()` 解析多格式，> 9天丢弃 | 运行中 | `run_intel.py` |
| L1 URL | `seen_urls.json` 90天TTL | 运行中 | `run_intel.py` |
| L2 标题 Jaccard | 阈值 0.45，年份保留，比对历史 `article_cache.json` | 运行中 | `dedup_utils.py` |
| L2.5 Gemma 31B IT（OR, reasoning off） | 时效/相关性/跨周去重（注入上周报告）/信息量，`max_tokens=4096`，`response_format=json_object` | 运行中 | `run_intel.py` |
| L3 MemPalace 语义 | BM25+cosine 混合，`source_contains='china-companies'` | 未启用 | `dedup_utils.py` |

**日期解析覆盖格式**：ISO `2026-05-20T...` / 中文绝对 `2025年3月25日` / 中文相对 `3天前`、`2周前`、`1个月前` / 英文相对 `3 days ago`、`2 weeks ago`。无法解析则 pass-through（不丢弃）。

**跨周去重机制**：`load_last_report_section()` 直接读 Obsidian 历史文件提取公司节（`## {zh}` → 首个 `---`），注入 prefilter prompt，指示模型剔除与上周内容高度重叠的文章。不依赖 MemPalace bridge，更可靠。列目录对 iCloud Drive 做 EINTR 短重试，耗尽则跳过本家上周节。

**L3 启用条件**：积累 3 个月以上历史报告后，可在 `run_intel.py` 启用 `find_report_duplicate()` 作为独立层叠加。

---

## LLM 模型选型

| 场景           | 模型                    | 路由              | 原因                       |
| ------------ | --------------------- | --------------- | ------------------------ |
| 情报主合成        | `deepseek-v4-pro`     | DeepSeek 直连 API | 深度推理，`reasoning_effort=high`，成本 ~$0.435/$0.87/M |
| Prefilter 门控 | `google/gemma-4-31b-it` | OpenRouter | 零推理结构化门控，`reasoning.enabled=false`；Crusoe/Friendli/OpenInference，ignore Together，`allow_fallbacks=false` |
| 邮件指令解析       | `openai/gpt-oss-20b`  | OpenRouter      | 响应快，指令理解准确               |
| 英文名推断        | `openai/gpt-oss-20b`  | OpenRouter      | 同上                       |
| Followup 推理  | `deepseek-v4-pro`     | DeepSeek 直连 API | 深度推理，`reasoning_effort=high` |

**DeepSeek content=null 处理**：推理模型先写 `reasoning_content` 字段，`content` 可能为空。所有 DeepSeek 调用点统一调用 `http_utils.extract_llm_text(msg)`（`content or reasoning_content or reasoning`），不再各自手写取值链——`prefilter_articles()` 曾因手写链漏掉 `reasoning_content` 导致门控静默 pass-through（2026-07-15，issue #10）

**LLM JSON 输出解析（`http_utils.call_llm_json()`）**：所有要求模型输出 JSON 的调用点（`prefilter_articles()`、`_parse_command()`、`_followup_three_stage()` stage1）统一调用 `call_llm_json(url, *, headers, json_body, timeout, logger, label, max_attempts=2)`，内部把"取字典字段 + 解析 JSON"收进同一个 try/except，校验解析结果为 dict，且只在"调用成功但输出质量差"（缺字段/空内容/解析失败/非 dict）时重试，网络层失败（`post_with_retry` 已内部重试）不再套第二层重试。三处调用点均带 `response_format: {"type": "json_object"}`，从 API 层面约束输出格式（DeepSeek 和 OpenRouter/`gpt-oss-20b` 均已验证支持）。不要在新调用点各自手写取值/解析/重试逻辑（2026-07-19，issue #13，事故详情见 `PITFALLS.md` #29）

---

## 搜索与内容获取层

### 主搜索：三级 fallback（`search()`）

前一级失败自动降级，全部失败抛 `RuntimeError`。

| 级别 | Provider | 参数 | 限额 |
|------|---------|------|------|
| 1 | Tavily | `topic=general, days=8, max_results=8`，返回 `published_date` | 10次/日 |
| 2 | SerpApi | Google 搜索，`num=8` | 250次/月 |
| 3 | Serper.dev | Google 搜索，`num=8` | 按量 |

### 补充搜索：中文新闻（`search_cn()`）

失败返回 `[]`（非致命），Serper News → SerpApi News fallback。

| 级别 | Provider | 参数 | 说明 |
|------|---------|------|------|
| 1 | Serper News | `POST /news, hl=zh-cn, gl=cn` | 新浪财经/东方财富/腾讯财经/界面新闻等，带发布日期 |
| 2 | SerpApi News | `tbm=nws, hl=zh-CN, tbs=qdr:w` | Google 新闻，过去一周 |

### 全文提取：Jina Reader（`_jina_extract()`）

`GET https://r.jina.ai/{url}`，header `Authorization: Bearer JINA_API_KEY`，返回 markdown 全文。

- **触发条件**：无 `published_date` 的文章（英文 Tavily 结果，snippet ~300字）
- **不触发**：Serper News 中文文章（有日期，snippet 已够定位）
- **上限**：每公司 `JINA_ENRICH_MAX=5` 次，避免超速率
- **失败处理**：静默回退到原始 snippet（paywall / 反爬 / 404 均正常失败）
- **内容截断**：Jina 全文存入 cache（`JINA_CACHE_CHARS=4000`），传给 LLM 截至 `JINA_LLM_CHARS=2000`；普通 snippet 仍用 500 字

### 时效控制

- **方案 A**：`SEARCH_DAYS = 8`（Tavily 索引窗口）
- **方案 B**：`_parse_pub_date()` 解析多格式发布日期，> `PUB_DATE_MAX_AGE=9` 天丢弃；无法解析 pass-through

**关键限制**：Tavily `days` 过滤的是**再索引时间**，不是文档发布日期。旧文章被重新爬取后仍在窗口内，必须靠方案 B 的日期解析二次过滤。

**Tavily topic 注意**：必须用 `topic=general`，`topic=news` 对中文公司名查询返回完全不相关英文新闻。

---

## 邮件系统

### 发信（Resend API）

```
email_sender.py → POST https://api.resend.com/emails
from:     Hermes MI <MI@physicalclue.us>
reply_to: MI@physicalclue.us
auth:     RESEND_API_KEY (Bearer)
```

- 支持 Markdown → HTML 内联渲染
- 回复邮件携带 `In-Reply-To` + `References` header
- JMAP 返回的 messageId 已剥除 `<>`，发送时需补齐：`f"<{msg_id}>"`

### 收信（Stalwart JMAP）

```
email_check.py → POST https://oci.physicalclue.us:8443/jmap/
auth:     STALWART_API_KEY (Bearer，永久有效，无需 OAuth)
account:  JMAP_ACCOUNT_ID = "c"  (mi@physicalclue.us)
inbox:    JMAP_INBOX_ID = "a"    (INBOX mailbox ID，已验证)
```

- 按游标增量拉取（回退 10 分钟重叠），分页 limit=50；游标缺失时退化为过去 24 小时
- `processed_email_ids.json`（72小时TTL）防重复处理

---

## 配置管理（watchlist.md）

**路径**：`{OBSIDIAN_PATH}/Hermes/MI/watchlist.md`

**格式**：
```markdown
## companies
# 格式：中文名 | English Name（可空）
比亚迪 | BYD
海尔集团 | Haier Group

## recipients
you@example.com
```

**优先级**：`watchlist.md` → `intel_config.yaml`（fallback）

**双写**：所有写操作同时更新两个文件（`config_store.save_config()`）

**parser 注意**：`## companies` 和 `## recipients` 的 heading 匹配优先于 `#` 注释过滤，否则 heading 被当注释跳过。

**writer 注意**：四状态机（pre→companies→recipients→post），只替换受控区块，frontmatter 和其他内容不动。

---

## MemPalace Bridge API（port 8765）

**Bridge 文件**：`~/.hermes/mempalace_bridge.py`（宿主机常驻，launchd `com.hermes.mempalace-bridge`）

MI 脚本通过 `http://localhost:8765` 访问。

| 端点 | 方法 | 用途 | 调用方 |
|------|------|------|--------|
| `/mempalace/search`（`/search` 为同义别名） | POST | MemPalace 语义搜索 | `memory_context.py`（`/mempalace/search`）、`dedup_utils.py`（`/search`，L3 未启用） |
| `/obsidian/search` | POST | Obsidian 全文搜索 | `memory_context.py` |
| `/obsidian/read` | POST | 读取笔记 | `memory_context.py` |
| `/mcp` | POST | MCP Streamable HTTP（供 Hermes Agent 用） | Hermes Gateway |

**搜索算法**：BM25（权重 0.4）+ cosine vector（权重 0.6）+ closet ranking boost

---

## 调度（launchd）

| plist | 周期 | 脚本 | 日志 |
|-------|------|------|------|
| `com.hermes.intel` | 周日 08:59 PDT | `~/MI/run_intel.py` | `~/MI/logs/intel.log`（30天滚动，见 log_utils.py） |
| `com.hermes.emailcheck`（已停用 2026-10-08） | 每 5 分钟 | `~/MI/email_check.py` | `~/MI/logs/emailcheck.log`（30天滚动） |
| `com.hermes.mi-slack-check`（已停用 2026-10-08） | 每 5 分钟 | `~/MI/slack_check.py` | `~/MI/logs/slack-check.log`（30天滚动） |
| `com.mi.sama-relay`（已停用） | 每小时 | `~/MI/sama_relay.py` | `~/MI/logs/sama_relay.log` |
| `com.hermes.mempalace-bridge` | 常驻 | `~/.hermes/mempalace_bridge.py` | `/tmp/hermes_mempalace_bridge.log` |

停用的 plist 移在 `~/Library/LaunchAgents/.disabled/`，恢复时移回并 `launchctl bootstrap`。

**`~/.hermes/cron/jobs.json`**：`"jobs": []`（空数组），防止 Hermes Agent 意外激活旧任务。

**手动触发**：
```bash
~/MI/.venv/bin/python ~/MI/run_intel.py
~/MI/.venv/bin/python ~/MI/run_intel.py --force   # 绕过去重
~/MI/.venv/bin/python ~/MI/email_check.py
```

---

## 环境变量（~/MI/.env）

| 变量 | 用途 |
|------|------|
| `OPENROUTER_API_KEY` | LLM 调用（prefilter Gemma 31B IT；邮件指令 gpt-oss-20b） |
| `DEEPSEEK_API_KEY` | LLM 调用（deepseek-v4-pro 直连合成；prefilter 已不走 DeepSeek） |
| `TAVILY_API_KEY` | 情报抓取主力（10次/日） |
| `SERPAPI_API_KEY` | Tavily 耗尽后备用（250次/月） |
| `SERPER_API_KEY` | 三级 fallback（Serper.dev） |
| `RESEND_API_KEY` | 邮件发送（`re_...`，永久有效） |
| `RESEND_STATUS_KEY` | 送达/退信状态查询（Full access，与发信 key 分离，`sama_relay.py` 用） |
| `FROM_ADDRESS` | 发件地址（缺失会导致发信静默失败，见开发日志 2026-07-06） |
| `STALWART_API_KEY` | JMAP 收信认证（`API_...`，永久有效） |
| `JMAP_BASE` / `JMAP_ACCOUNT_ID` / `JMAP_INBOX_ID` | JMAP 服务器地址、账号 ID、INBOX ID（缺失即 `JMAPConfigError`） |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot（`{bot_id}:{secret}`） |
| `TELEGRAM_ALLOWED_USERS` | 允许接收通知的用户 chat ID（逗号分隔） |
| `TELEGRAM_HOME_CHANNEL` | 通知目标 chat（优先；缺省取 `TELEGRAM_ALLOWED_USERS` 第一个） |
| `HERMES_DATA` | 数据目录，默认 `~/MI/data` |
| `JINA_API_KEY` | Jina Reader 全文提取（`jina_...`，免费 500 RPM） |
| `OBSIDIAN_PATH` | Obsidian vault 绝对路径 |
| `SLACK_BOT_TOKEN` | Slack Bot Token，与 Hermes 共用（`xoxb-...`） |
| `SLACK_MI_CHANNEL` | Slack 投递频道 ID（`C0B5X97Q879`，`#china-corp-intel`） |
| `SLACK_ALLOWED_USERS` | 允许发 MI 追问的 Slack user ID（逗号分隔） |
| `GITHUB_TOKEN` | `sama_relay.py` 读写 GitHub issue |

---

## 关键常量与阈值

| 常量 | 值 | 位置 |
|------|----|------|
| `SEARCH_DAYS` | 8 | `run_intel.py` |
| `PUB_DATE_MAX_AGE` | 9 天 | `run_intel.py` |
| `JINA_ENRICH_MAX` | 5 条/公司 | `run_intel.py` |
| `JINA_CACHE_CHARS` | 4000 | `run_intel.py` |
| `JINA_LLM_CHARS` | 2000 | `run_intel.py` |
| `SEEN_URL_TTL_DAYS` | 90 | `run_intel.py` |
| L2 Jaccard 阈值 | 0.45 | `dedup_utils.py:find_cache_duplicate()` |
| L3 语义阈值 | 0.65 | `dedup_utils.py:find_report_duplicate()` |
| Prefilter max_tokens | 4096 | `run_intel.py:prefilter_articles()` |
| Synthesis max_tokens | 8000 | `run_intel.py:synthesize_with_llm()` |
| Followup stage1 max_tokens | 2048 | `email_check.py:_followup_three_stage()` |
| Followup stage3 max_tokens | 2000 | `email_check.py:_followup_three_stage()` |
| JMAP 游标重叠窗口 | 10 分钟（游标缺失时退化为 24 小时） | `email_check.py:CURSOR_OVERLAP_MINUTES` |
| processed_ids TTL | 72 小时 | `email_check.py:_load_processed_ids()` |
| KB context 上限 | 1500 字 | `memory_context.py` |
| 历史 context 条数上限 | 8 条 | `run_intel.py` |

---

## 数据文件（~/MI/data/）

| 文件 | 内容 | 结构 | TTL |
|------|------|------|-----|
| `seen_urls.json` | 已处理 URL | `{url: {ts, company}}` | 90天 |
| `fetch_log.json` | 每公司每日抓取记录 | `{zh: date_str}` | 无，按日覆盖 |
| `article_cache.json` | 文章全文缓存（含 Jina 丰富后内容） | **flat dict** `{url: {title, content, company, ts}}` | 90天 |
| `processed_email_ids.json` | 已处理邮件 ID | `{id: ts}` | 72小时 |
| `jmap_cursor.json` | JMAP 轮询游标（最新 `receivedAt`） | JSON | 无 |
| `processed_slack_ts.json` | 已处理 Slack 消息 ts | `{ts: t}` | 72小时 |
| `slack_last_check.json` | Slack 轮询游标 | `{"ts": ...}` | 无 |
| `sama_relay_state.json` | sama relay 上次发送记录（退信核查用） | JSON | 无 |
| `intel_config.yaml` | 公司+收件人 fallback 配置 | YAML | 无 |
| `email_actions.log` | 邮件指令执行日志（追加） | 文本 | 无 |

---

## 已知行为与边界情况

**推理模型 content=null / 空内容**：推理模型可能在 reasoning 阶段耗尽 token 导致 `content` 为空，或围栏内容为空。prefilter 曾用 DeepSeek V4 Flash 频繁触发，2026-09-01 起改用 Gemma 31B IT + reasoning off（PITFALLS.md #31）；合成与 followup 仍用 V4 Pro，取值统一走 `extract_llm_text()`，JSON 调用统一走 `call_llm_json()`（自动重试坏输出 + 诊断日志 raw 原文）。仍属已知风险——LLM 输出本身是概率性的，无法保证归零。

**Tavily `days` 过滤的是索引时间**：旧文章被重新爬取后仍出现在窗口内，必须靠 `_parse_pub_date()` + `PUB_DATE_MAX_AGE` 二次过滤。

**Jina paywall / 反爬失败**：主流英文财经媒体（Reuters、SCMP、Bloomberg、Yahoo Finance、Moomoo）对 Jina 有 paywall 或反爬保护。`_jina_extract()` 仅在 `len(full_text) > len(original_snippet)` 时才替换，防止无效页面替换有意义的原始 snippet。

**无日期文章不等于可保留**：Prefilter prompt 明确要求"无日期文章需根据内容判断时效，不得默认保留"。

**`--force` 模式行为**：不检查 `seen_urls.json`；不写回 `seen_urls.json`；跳过 L2 title dedup。**但仍会写入 `article_cache.json`**。多次 `--force` 运行会累积 cache 条目，影响后续正式运行的 L2 去重。

**dry run 后重跑的正确清除步骤**：
1. `fetch_log.json` 今日条目
2. `seen_urls.json` 今日写入条目（按 `ts >= today_ts` 过滤）
3. `article_cache.json` 今日写入条目（按 `ts >= today_ts` 过滤，flat dict 结构）
4. Obsidian 今日报告文件

**异步线程必须 daemon=False**：launchd 在 `run_email_check()` 返回后立即 exit，daemon 线程会被 kill。所有后台线程收集到模块级 `_background_threads`，函数末尾 join + clear。

**Slack 追问同样先确认投递再标记处理**：`slack_check.py` 有效追问路径先跑 pipeline + `post_message`，仅当 `post_message` 返回非空 ts 才写 `processed_slack_ts`/游标；跳过类消息（机器人/非白名单/父消息非 bot）无副作用，标记时机不变（2026-07-15，issue #8）。

**关键 env 缺失一律 fail-fast**：`email_check.py`（`JMAPConfigError`）、`run_intel.py`（`IntelConfigError`：`TAVILY_API_KEY`/`DEEPSEEK_API_KEY`/`OPENROUTER_API_KEY`/`HERMES_DATA`/`OBSIDIAN_PATH`）、`slack_check.py`（`SlackConfigError`：`SLACK_BOT_TOKEN`/`SLACK_MI_CHANNEL`）均在入口函数最开头校验必需变量，缺失即抛专用异常，逃逸到 `__main__` 触发非零 exit，不再静默 `return` 让 launchd 误判为成功（2026-07-15，issue #11）。

**Resend 域名限制**：只能从 `@physicalclue.us` 根域发信，`@send.physicalclue.us` 子域未验证（返回 403）。

**JMAP messageId 格式**：Stalwart 返回的 messageId 已剥除 `<>`，发送 `In-Reply-To` header 时需手动补齐：`f"<{msg_id}>"`。

**bot_user_id ≠ chat_id**：Telegram Bot token 前缀（冒号前）是 bot 自身 ID，不是用户 chat ID。用户 chat ID 来自 `TELEGRAM_ALLOWED_USERS`。

---

## 待办：Slack 追问实时性（2026-05-26）

### [P2] Slack 追问从轮询改为实时推送

**搁置（2026-10-08）**：`slack_check.py` 的 launchd 轮询已停用，恢复 Slack 追问时再决策。

**当前状态**：`slack_check.py` 每 5 分钟轮询，对 Slack 用户而言延迟过高。

**根本约束**：Hermes 已通过 Socket Mode 占用唯一 WebSocket 连接，MI 无法对同一 Slack App 再开一个 Socket Mode 连接。

**三个备选方案**：

| 方案 | 机制 | 延迟 | 代价 |
|------|------|------|------|
| A：Hermes 路由 | MI 暴露 MCP tool（port 8644），Hermes 收 `mi:` 消息后调用 MI pipeline | ~10-30s | 每次多一个 Hermes LLM call；LLM routing 不 100% 可靠 |
| B：独立 Slack App | 给 MI 新建专用 Slack App，独立 Socket Mode | 毫秒级 | 需手动在 api.slack.com 创建 App |
| C：30s 轮询 | `StartInterval` 从 300 改为 30 | 30s | 几乎无代价，但 Slack 体验仍偏慢 |
