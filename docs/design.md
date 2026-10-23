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
   intel_sources.collect_company() ───────────► 东方财富公告（列表 + 正文） + Exa（4 次/家）
   intel_sources.collect_industry() ──────────► Exa（1 次/行业）
         |
   intel_clean.clean_company() ───────────────► 时间窗 / seen_urls / 农场与屏蔽域名 / 例行公告 / 低价值 / 集团别名归属 / 标题聚类去重
         |
   intel_clean.classify_two_stage() ──────────► OpenRouter Haiku 5.5 初筛 + GPT-6 Luna 复核，取严
         |
   load_last_report_section() + memory_context.py ─► 上周本节 + MemPalace/Obsidian 背景
         |
   intel_synth.build_material() ──────────────► 只收公告与正文报道
   intel_synth.compose_section() ─────────────► Haiku 起草 → Luna 事实核查 → Haiku 修订；留档 logs/audit-*.md
         |
   ┌─────────────────────────────────────────────────────────┐
   │  Obsidian vault / Hermes/MI/YYYY-MM-DD-china-companies  │
   └─────────────────────────────────────────────────────────┘
         |
   email_sender.py ────────────────────────────► Resend API → 收件人
   _send_telegram_alert() ─────────────────────► Telegram Bot API
   slack_sender.post_report() ─────────────────► Slack API → SLACK_MI_CHANNEL
         |
   intel.log：每家一行 FUNNEL，每次运行一行 COST

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
    ├── _followup_three_stage()（分析提问）─────► OpenRouter gpt-oss-20b + search_utils + DeepSeek V4 Pro 直连
    └── run_intel()（立即生成）─────────────────► 同上情报主循环
         |
   email_sender.py ────────────────────────────► Resend API → 发件人
```

---

## 数据流：情报主循环（run_intel.py）

规则与阈值集中在 `config/intel_rules.yaml`，集团主体映射在 `config/entity_map.yaml`（两份都由 `intel_sources` 在 import 时读入）。

1. **配置校验**：`_validate_intel_config()` 要求 `EXA_API_KEY`、`OPENROUTER_API_KEY`、`HERMES_DATA`、`OBSIDIAN_PATH` 非空，缺失抛 `IntelConfigError`，非零退出
2. **配置加载**：从 `watchlist.md`（优先）或 `intel_config.yaml`（fallback）读取公司列表和收件人
3. **频率控制**：`fetch_log.json` 记录每公司每日抓取状态，`--force`/`--sample` 绕过
4. **行业上下文**：按各公司 `entity_map` 的 `industries` 去重后，每个行业查 1 次 Exa，经窗口/seen/农场/低价值/聚类去重后用行业版标签做两段式分类，只留 keep
5. **跨周上下文**：`load_last_report_section()` 从 Obsidian 最近一期报告取该公司节（`## {zh}` 到下一个 `---` 或 `## `，截至 `synth.last_week_chars=3000`），注入合成 prompt，要求不重复上周内容。iCloud vault 的 `glob` 遇 `InterruptedError`/`EINTR` 短重试，耗尽返回 `""`，不打死整轮（issue #15，PITFALLS #30）
6. **采集**（`collect_company`）：
   - 东方财富公告：`entity_map` 里每个带 `filings` 代码的主体拉一次公告列表（A 股/港股按代码位数选 `ann_type`），窗口内保留
   - Exa：按 `exa.queries` 顺序查 4 次，每次 `num_results=20`、正文 `max_characters=2000`、`startPublishedDate` = 窗口起点，不带 `category`。`综合媒体` 用 `includeDomains=exa.media_domains`（33 个财经与行业媒体），`综合开放`、`数字化运营`、`战略竞争` 用 `excludeDomains=blocked_domains`。`综合*` 不打维度标签，其余以查询名为维度。未在 `entity_map` 里的公司只跑两个 `综合*` 查询
   - 正文不足 300 字的 Exa 结果记为 `headline`，只参与清洗，不进合成材料
   - 所有调用都出错才视为本家失败（不写 fetch_log，下次重试）
7. **清洗**（`clean_company`）：发布日期在 `window_days=8` 内（无日期丢弃）→ 未见过（`seen_urls`）→ 非农场页/屏蔽域名 → 公告排除例行类（`filings.routine_patterns`），报道排除低价值标题（`low_value_patterns`）→ 报道须命中集团或下属主体别名（`exclude` 词先剔除，`require_comention` 主体须同时命中集团别名）→ 标题二元组 Jaccard ≥ 0.6 聚类，每簇留一条（公告 > 正文报道 > 标题，同级取最早）
8. **两段式分类**（`classify_two_stage`）：每批 40 条，`anthropic/claude-haiku-5.5` 初筛与 `openai/gpt-6-luna` 复核各给 keep/mention/promo/unrelated，取更严的标签，只留 keep 与 mention；任一模型失败则该批丢弃并计入 `classify_failed`
9. **公告正文**：按时间倒序取前 `filings.bodies_max=10` 条拉东方财富正文（去掉港股免责声明头）
10. **知识库注入**：`memory_context.py` 调 bridge 拉 MemPalace 语义搜索 + Obsidian 全文搜索背景，标为"仅供背景参考，不是本周事实"
11. **组材料**（`build_material`）：【集团范围】、【公司公告】（≤10，带正文的附 2500 字）、【报道·正文】（keep 在前、同组按时间倒序，≤15 篇，每篇 1800 字，带来源层级与维度）、【行业与竞争】（≤3 篇正文）。只有标题或正文为空的报道一律不进材料
12. **合成**（`compose_section`，全部走 OpenRouter）：
    - 起草：Haiku 5.5，`reasoning.effort=medium`，`max_tokens=16000`，全文 ≤500 字；`finish_reason=length` 的截断稿丢弃，以 `low` 重试一次，仍失败则本节跳过
    - 核查：Luna，`effort=medium`，`response_format=json_object`；要求草稿里每个数字、日期、人名、金额都能在材料里找到原文依据，日期/主体/出处错配也算错，问题分 `unsupported`/`contradicted`
    - 修订：有问题时 Haiku `effort=low` 只改被标出的句子，能按材料更正就更正，否则删掉；核查调用失败时保留草稿
    - 留档：每家写 `logs/audit-<运行时间>-<公司>.md`（材料、草稿、核查意见、终稿四节），30 天后随日志清理；写入失败只记 warning
13. **输出**：Obsidian 存档带 frontmatter、采集/模型说明、费用和任务 ID；邮件与 Slack 只有标题、各公司节和 footer，公司之间一条分隔线
14. **样文模式**（`--sample`）：不写 Obsidian、`seen_urls`、`fetch_log`，不发 Telegram/Slack，邮件标题带 `[样文]` 并发给全部收件人
15. **日志**：每家一行 `FUNNEL {...}`（raw_eastmoney/raw_exa、各过滤层计数、分类结果、material_chars）；每次运行结束（含无新情报、异常、无公司配置）在 `finally` 里写一行 `COST total=$… openrouter=$… exa=$…`
16. **失败隔离**：单家异常记入"跳过"并继续；全家失败且无 sections 时抛错非零退出，不发「无新情报」

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
| `run_intel.py` | 情报主循环入口：行业上下文、逐家采集/清洗/分类/合成、输出与投递、FUNNEL 与 COST 日志；`--force`/`--sample`/`--recipients` |
| `intel_sources.py` | 东方财富公告列表与正文、Exa 查询；`collect_company()`/`collect_industry()`；`SourceStats` 记调用数、错误数、Exa 费用 |
| `intel_clean.py` | 窗口、农场、例行公告、低价值、集团别名归属、标题聚类去重；`classify_two_stage()` 两段式分类 |
| `intel_synth.py` | `build_material()` 组材料；`compose_section()` 起草/核查/修订与留档；三个 prompt |
| `config/intel_rules.yaml` | 信源、过滤、材料上限、模型与推理强度等全部规则 |
| `config/entity_map.yaml` | 集团 → 上市主体/子公司/品牌/参股，别名、排除词、公告代码、所属行业（草稿，待负责人确认） |
| `email_check.py` | 邮件轮询、指令解析与执行（launchd 已停用 2026-10-08，可手动运行） |
| `email_sender.py` | Resend API 封装，Markdown → HTML |
| `slack_sender.py` | Slack 发送：md_to_slack 转换 + 超长分块串 thread；`post_report()` 读 `SLACK_MI_CHANNEL` |
| `slack_check.py` | Slack 轮询（launchd 每 5 分钟，已停用 2026-10-08）：thread 回复 bot 消息 或 `mi: ` 前缀 → followup pipeline |
| `search_utils.py` | 追问用三级搜索 fallback（Tavily→SerpApi→Serper）；周报不再使用 |
| `config_store.py` | watchlist.md + intel_config.yaml 读写 |
| `http_utils.py` | httpx 重试封装（网络错误/5xx 重试，4xx 不重试）；`extract_llm_text()`；`call_llm_json()` 统一封装"调用 LLM + 取值 + 解析 JSON"，仅对坏输出重试 |
| `memory_context.py` | 每公司合成前注入历史上下文（bridge REST）：三路 MemPalace 语义搜索 + Obsidian 全文搜索 |
| `hermes_footer.py` | 邮件 footer |
| `log_utils.py` | `setup_logging()`：`~/MI/logs/` 下 `TimedRotatingFileHandler`，30 天滚动；`LOG_DIR` 下 30 天前的文件（含 audit）启动时清理 |
| `article_cache.py` / `dedup_utils.py` | 旧管线的文章缓存与 L2/L3 去重，周报已不调用；`test_dedup.py`、`run_intel_deepseek_test.py` 仍引用 |
| `manual_send_test_email.py` | 手动发测试邮件（原 `test_email.py`，改名防 pytest 收集时真实发信） |
| `test_intel_*.py` / `test_run_intel_pipeline.py` / `test_eintr_retry.py` / `test_jmap_config.py` / `test_no_send_on_import.py` | 单测（全部离线，不调网络） |
| `gmail_client.py` | **DEPRECATED** — Gmail OAuth2 封装，不再使用 |
| `sama_relay.py` | 把 `PhysicalClue611/PC611-homepage` 中标题含 `[sama]` 的最早一条 open issue 转发成邮件，成功后 close；launchd `com.mi.sama-relay` 已停用 |
| `run_intel_deepseek_test.py` | 一次性测试脚本（旧管线），非生产路径 |

---

## 去重架构

| 层 | 机制 | 位置 |
|---|---|---|
| 时间窗 | 发布日期在 `window_days=8` 内；无日期一律丢弃；Exa 请求本身也带 `startPublishedDate` | `intel_clean.in_window` |
| URL | `seen_urls.json` 90 天 TTL，正式运行把本周进入分类后的公告与报道 URL 写回 | `run_intel.py` |
| 本周聚类 | 标题二元组 Jaccard ≥ 0.6（或标题相同）并查集聚类，每簇留一条 | `intel_clean.dedupe` |
| 跨周 | 上周本节注入合成 prompt，要求只写新进展 | `load_last_report_section()` |

旧管线的 `article_cache.json` L2 去重、Gemma prefilter、MemPalace L3 均已不在周报路径上。

---

## LLM 模型选型

| 场景 | 模型 | 路由 | 参数 |
|---|---|---|---|
| 分类初筛 | `anthropic/claude-haiku-5.5` | OpenRouter | `reasoning.effort=low`，`json_object`，批 40 |
| 分类复核 | `openai/gpt-6-luna` | OpenRouter | 同上，两者取严 |
| 周报起草 / 修订 | `anthropic/claude-haiku-5.5` | OpenRouter | 起草 `medium`、修订 `low`、截断重试 `low`，`max_tokens=16000` |
| 事实核查 | `openai/gpt-6-luna` | OpenRouter | `medium`，`json_object` |
| 邮件指令解析 / 英文名推断 | `openai/gpt-oss-20b` | OpenRouter | 响应快 |
| Followup 推理 | `deepseek-v4-pro` | DeepSeek 直连 | `reasoning_effort=high` |

负责人决定：起草用 Haiku、核查用 Luna，不用 Claude Sonnet/Opus（成本）。模型与强度都在 `config/intel_rules.yaml` 的 `classify`/`synth` 下。

**费用记录**：OpenRouter 调用带 `usage: {include: true}`，`usage.cost` 累加；Exa 取响应 `costDollars.total`（为 null 记 0）。两者在 COST 行与 Obsidian 存档头里合计。

**推理模型取值**：所有调用点统一 `http_utils.extract_llm_text(msg)`（`content or reasoning_content or reasoning`）。

**LLM JSON 输出解析（`http_utils.call_llm_json()`）**：所有要求 JSON 的调用点（分类、核查、`_parse_command()`、`_followup_three_stage()` stage1）统一走 `call_llm_json`，带 `response_format: {"type": "json_object"}`，只在"调用成功但输出质量差"时重试，网络层失败不再套第二层重试（issue #13，PITFALLS #29）。

---

## 搜索与内容获取层

### 周报信源

| 信源 | 用法 | 费用 |
|---|---|---|
| 东方财富公告 | `np-anotice-stock.eastmoney.com/api/security/ann` 列表 + `np-cnotice-stock.eastmoney.com/api/content/ann` 正文 | 免费 |
| Exa `/search` | `type=auto`，不带 `category`；白名单查询用 `includeDomains`，其余用 `excludeDomains`；`contents.text.maxCharacters=2000` | `auto` 每千次 $7（含 10 条）+ 超出部分每千条 $1，20 条约 $0.017/次，正文不另计；免费层每月 $10（2026-10-23 官方定价页） |

每周约 6 家 × 4 + 6 行业 × 1 ≈ 30 次 Exa。2026-10-23 样文实测一次运行 `exa=$0.426`、`openrouter=$0.099`。

**选型依据与未尽事宜**见仓库 `docs/playbooks/intel_source_evaluation.md`：Google News RSS 只有标题、导致"把标题写成事实"，已弃用；Serper/SerpApi News 同一索引；Tavily 中文无效；Parallel 日期缺失多、只有前 10 条带摘录，不用；国内 AI 搜索 API（博查、智谱、千帆、秘塔）未测。

### 追问用搜索（`search_utils.py`，周报不用）

`search()`：Tavily（`topic=general, days=8`）→ SerpApi → Serper，前一级失败自动降级。Tavily 必须用 `topic=general`，`topic=news` 对中文公司名返回无关英文新闻；`days` 过滤的是再索引时间，不是发布日期。

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

**集团映射与采集规则**：watchlist 里的公司名对应 `config/entity_map.yaml` 的集团键（主体、别名、排除词、公告代码、行业）；未映射的公司只用中英文名做两次综合 Exa 查询。全部过滤与模型参数在 `config/intel_rules.yaml`。

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
| `EXA_API_KEY` | 周报 Exa 采集（必需，缺失即 `IntelConfigError`） |
| `OPENROUTER_API_KEY` | 周报分类/起草/核查/修订；邮件指令 gpt-oss-20b（必需） |
| `PARALLEL_API_KEY` | Parallel Search，2026-10-23 评估后未接入代码，仅保留 |
| `DEEPSEEK_API_KEY` | Followup 推理（deepseek-v4-pro 直连）；周报不用 |
| `TAVILY_API_KEY` / `SERPAPI_API_KEY` / `SERPER_API_KEY` | 追问搜索三级 fallback；周报不用 |
| `JINA_API_KEY` | 旧管线全文提取，周报不用（账户无余额，2026-10-23 搜索接口返回 402） |
| `RESEND_API_KEY` | 邮件发送（`re_...`，永久有效） |
| `RESEND_STATUS_KEY` | 送达/退信状态查询（Full access，与发信 key 分离） |
| `FROM_ADDRESS` | 发件地址（缺失会导致发信静默失败，见开发日志 2026-07-06） |
| `STALWART_API_KEY` | JMAP 收信认证（`API_...`，永久有效） |
| `JMAP_BASE` / `JMAP_ACCOUNT_ID` / `JMAP_INBOX_ID` | JMAP 服务器地址、账号 ID、INBOX ID（缺失即 `JMAPConfigError`） |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot（`{bot_id}:{secret}`） |
| `TELEGRAM_ALLOWED_USERS` | 允许接收通知的用户 chat ID（逗号分隔） |
| `TELEGRAM_HOME_CHANNEL` | 通知目标 chat（优先；缺省取 `TELEGRAM_ALLOWED_USERS` 第一个） |
| `HERMES_DATA` | 数据目录，默认 `~/MI/data`（必需） |
| `OBSIDIAN_PATH` | Obsidian vault 绝对路径（必需） |
| `SLACK_BOT_TOKEN` | Slack Bot Token，与 Hermes 共用（`xoxb-...`） |
| `SLACK_MI_CHANNEL` | Slack 投递频道 ID（`C0B5X97Q879`，`#china-corp-intel`） |
| `SLACK_ALLOWED_USERS` | 允许发 MI 追问的 Slack user ID（逗号分隔） |
| `GITHUB_TOKEN` | GitHub REST（`sama_relay.py`；仓库操作一律用它，不用 gh CLI） |
| `GITHUB_REVIEWER_TOKEN` | 评审账号（blacktomb42）提交 PR review |

---

## 关键常量与阈值

周报相关的全部在 `config/intel_rules.yaml`：

| 键 | 值 |
|---|---|
| `window_days` | 8 |
| `exa.num_results` / `exa.max_characters` | 20 / 2000 |
| `exa.media_domains` | 33 个 |
| `filings.bodies_max` / `filings.body_chars` | 10 / 2500 |
| `material.filings_max` / `bodies_max` / `body_chars` / `industry_bodies_max` | 10 / 15 / 1800 / 3 |
| `classify.batch_size` / `reasoning_effort` | 40 / low |
| `synth.draft_effort` / `check_effort` / `revise_effort` / `retry_effort` | medium / medium / low / low |
| `synth.max_tokens` / `last_week_chars` | 16000 / 3000 |
| 本周聚类 Jaccard | 0.6（`intel_clean.dedupe`） |
| Exa 正文判为 article 的下限 | 300 字（`intel_sources.fetch_exa`） |

其他：

| 常量 | 值 | 位置 |
|------|----|------|
| `SEEN_URL_TTL_DAYS` | 90 | `run_intel.py` |
| `EINTR_MAX_ATTEMPTS` | 4 | `run_intel.py` |
| 日志与留档保留 | 30 天 | `log_utils.py` |
| Followup stage1 / stage3 max_tokens | 2048 / 2000 | `email_check.py:_followup_three_stage()` |
| JMAP 游标重叠窗口 | 10 分钟（游标缺失时退化为 24 小时） | `email_check.py:CURSOR_OVERLAP_MINUTES` |
| processed_ids TTL | 72 小时 | `email_check.py:_load_processed_ids()` |
| KB context 上限 | 1500 字 | `memory_context.py` |

---

## 数据文件（~/MI/data/）

| 文件 | 内容 | 结构 | TTL |
|------|------|------|-----|
| `seen_urls.json` | 已处理 URL | `{url: {ts, company}}` | 90天 |
| `fetch_log.json` | 每公司每日抓取记录 | `{zh: date_str}` | 无，按日覆盖 |
| `article_cache.json` | 旧管线文章缓存，周报不再读写 | flat dict | 90天 |
| `processed_email_ids.json` | 已处理邮件 ID | `{id: ts}` | 72小时 |
| `jmap_cursor.json` | JMAP 轮询游标（最新 `receivedAt`） | JSON | 无 |
| `processed_slack_ts.json` | 已处理 Slack 消息 ts | `{ts: t}` | 72小时 |
| `slack_last_check.json` | Slack 轮询游标 | `{"ts": ...}` | 无 |
| `sama_relay_state.json` | sama relay 上次发送记录（退信核查用） | JSON | 无 |
| `intel_config.yaml` | 公司+收件人 fallback 配置 | YAML | 无 |
| `email_actions.log` | 邮件指令执行日志（追加） | 文本 | 无 |

`~/MI/logs/` 下另有 `intel.log`（FUNNEL/COST）和 `audit-<运行时间>-<公司>.md`（材料/草稿/核查/终稿），30 天清理。

---

## 已知行为与边界情况

**只有标题的条目不进材料**：2026-10-23 第二版样文把十几条 Google News 标题（丁少翔进亚玛芬董事会）写成确定事实且日期写错，核查也没拦住（PITFALLS #34）。现在 Google News 已移除，Exa 正文不足 300 字、或正文为空的报道一律不进合成材料。

**推理模型截断**：`finish_reason=length` 的生成一律丢弃，不返回半截文字（2026-10-23 样文曾把半截比亚迪一节发出）。

**根目录 `test_*.py` 不得有模块级副作用**：pytest 收集时会 import 执行。旧 `test_email.py` 因此给全部收件人真实发过信（PITFALLS #33），已改名 `manual_send_test_email.py`，`test_no_send_on_import.py` 守护。

**`--force` 模式**：不查也不写 `seen_urls.json`，不查 fetch_log，不注入上周本节；仍写 Obsidian 并投递。

**`--sample` 模式**：除邮件外无任何持久副作用（`seen_urls` 加载时也不回写过期清理），邮件发给全部收件人。

**正式运行后重跑的清除步骤**：`fetch_log.json` 今日条目、`seen_urls.json` 今日写入条目（按 `ts >= today_ts`）、Obsidian 今日报告文件。

**异步线程必须 daemon=False**：launchd 在 `run_email_check()` 返回后立即 exit，daemon 线程会被 kill。所有后台线程收集到模块级 `_background_threads`，函数末尾 join + clear。

**Slack 追问同样先确认投递再标记处理**：`slack_check.py` 有效追问路径先跑 pipeline + `post_message`，仅当 `post_message` 返回非空 ts 才写 `processed_slack_ts`/游标（2026-07-15，issue #8）。

**关键 env 缺失一律 fail-fast**：`email_check.py`（`JMAPConfigError`）、`run_intel.py`（`IntelConfigError`：`EXA_API_KEY`/`OPENROUTER_API_KEY`/`HERMES_DATA`/`OBSIDIAN_PATH`）、`slack_check.py`（`SlackConfigError`：`SLACK_BOT_TOKEN`/`SLACK_MI_CHANNEL`）均在入口函数最开头校验，缺失即抛专用异常，非零 exit（2026-07-15，issue #11）。

**Resend 域名限制**：只能从 `@physicalclue.us` 根域发信，`@send.physicalclue.us` 子域未验证（返回 403）。

**JMAP messageId 格式**：Stalwart 返回的 messageId 已剥除 `<>`，发送 `In-Reply-To` header 时需手动补齐：`f"<{msg_id}>"`。

**bot_user_id ≠ chat_id**：Telegram Bot token 前缀（冒号前）是 bot 自身 ID，不是用户 chat ID。用户 chat ID 来自 `TELEGRAM_ALLOWED_USERS`。

---

## 已知问题与待观察（2026-10-23）

负责人决定：新管线先运行一段时间，再决定是否继续优化。以下问题已记录、暂不修：

1. **篇幅超限**：prompt 要求全文 ≤500 字，2026-10-23 样文终稿安踏 721 字符、徐工 691，其余 510–580。修订只逐句改，不压缩全文。
2. **修订句措辞**：修订后的句子会出现"材料未显示……""所列报道未提及……"这类材料说明式措辞，违反写作规范。原因是 `REVISE_INSTRUCTION` 不带 `SYSTEM_PROMPT` 的写法要求。
3. **集团映射待确认**：`config/entity_map.yaml` 是草稿。子公司/品牌不再单独查询，只靠别名归属；若子公司动态漏得多，再考虑按实体加 Exa 查询。
4. **规模与成本**：扩到 10 家前要先解决时间（串行约 13 分钟）和成本；Exa 每月约 $2，免费层够用。
5. **信源**：某家连续几周材料过薄时，按 `docs/playbooks/intel_source_evaluation.md` 的基准评估国内 AI 搜索 API。

观察方法：每周看 `intel.log` 的 FUNNEL 与 COST，抽查 `logs/audit-*.md` 的核查意见与终稿。

---

## 待办：Slack 追问实时性（2026-05-26）

### [P2] Slack 追问从轮询改为实时推送

**已关闭（2026-10-08）**：邮件和 Slack 追问无人使用，不值得维持每 5 分钟一次的定时任务，`email_check.py`/`slack_check.py` 的 launchd 轮询已停用。未来需要追问能力时改用非轮询方式实现，不再恢复轮询，下表方案 C 不再考虑，仅留作当时的决策记录。

**当前状态**：`slack_check.py` 每 5 分钟轮询，对 Slack 用户而言延迟过高。

**根本约束**：Hermes 已通过 Socket Mode 占用唯一 WebSocket 连接，MI 无法对同一 Slack App 再开一个 Socket Mode 连接。

**三个备选方案**：

| 方案 | 机制 | 延迟 | 代价 |
|------|------|------|------|
| A：Hermes 路由 | MI 暴露 MCP tool（port 8644），Hermes 收 `mi:` 消息后调用 MI pipeline | ~10-30s | 每次多一个 Hermes LLM call；LLM routing 不 100% 可靠 |
| B：独立 Slack App | 给 MI 新建专用 Slack App，独立 Socket Mode | 毫秒级 | 需手动在 api.slack.com 创建 App |
| C：30s 轮询 | `StartInterval` 从 300 改为 30 | 30s | 几乎无代价，但 Slack 体验仍偏慢 |
