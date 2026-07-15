# MI 踩坑记录

`~/MI/CLAUDE.md` 的详细踩坑历史，从 CLAUDE.md 拆出以控制其体积。涉及 bug 排查、部署故障、静默降级等具体案例时应读取本文件；`CLAUDE.md` 只保留系统现状和指向本文件的指针。

新条目追加到文件末尾（早的在前，晚的在后），编号递增，不倒序插入。

---

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

### 22. JMAP 关键配置缺失仍是"静默降级"而非 fail-fast（已修复 2026-07-12，issue #6）
踩坑 #20 的故障模式（`.env` 缺关键变量 → 请求必然失败 → 异常被 catch 只记 ERROR → 进程 exit 0 → launchd 判定"成功"）在代码层面从未真正根除，只是把当时缺失的三个变量补齐了；只要哪天再缺（部署迁移、手误、权限变更），完全相同的静默失效会原样重演，且同样只能靠"功能是否真的在工作"主动验证才能发现。issue #6（由本机另一 AI 工具 Codex 用错误 GitHub 账号 `portfonia` 提交，见 [[feedback_github_auth]]）指出这个缺口。**修复**：`email_check.py` 新增 `_validate_jmap_config()`，在 `run_email_check()` 最开头校验 `JMAP_BASE`/`JMAP_ACCOUNT_ID`/`JMAP_INBOX_ID`/`STALWART_API_KEY` 四个必需变量非空，缺失即抛专用的 `JMAPConfigError`（日志只报变量名，不报值，避免泄露 key）；该异常在 fetch 的 `try/except` 之外，会逃逸到 `__main__` 层被 `raise`，进程以非零 exit code 退出，不再被吞掉。`STALWART_API_KEY` 从原来每次调用时 `os.getenv` 改为模块级常量，纳入统一校验。新增 `test_jmap_config.py` 覆盖缺 `JMAP_BASE`/缺 `STALWART_API_KEY`/错误消息不泄露密钥值三种场景。**教训**：静默降级模式一旦在某处出现，如果只修复触发它的那一次具体原因（补齐变量）而不修复"降级本身允许发生"的机制，同一类故障会在下一次触发条件出现时原样复现——修 bug 时要分清"这次的诱因"和"允许这类诱因造成后果的设计缺陷"，两者都要处理。

### 23. Slack 轮询游标在处理消息之前就写入，且不分页（已修复 2026-07-12，issue #7）
`slack_check.py` 的 `run_slack_check()` 原实现拉到 `conversations.history`（`limit=100`，不消费 `has_more`/`cursor`）后，**立即**把当前时间戳写入 `slack_last_check.json` 作为下次轮询起点，然后才开始 for 循环处理消息。这是比 issue #5（JMAP 24 小时窗口）更原始的写法——#5 至少是"处理完整批才可能丢"，这里是"游标写入和消息处理完全脱钩"：只要写入游标之后、处理完所有消息之前的任意一步抛出未捕获异常（网络错误、`post_message` 失败等），本轮已拉到但还没处理的消息会在下一轮查询里彻底消失，不会重试。再加上没有分页，两次轮询之间如果攒了超过 100 条消息（例如轮询本身中断了一段时间后恢复），超出的老消息在游标推进后同样永久不可恢复，且这部分连"重叠窗口"都救不了，因为压根没被拉取到。**修复**：`_get_channel_history` 改为跟随 `has_more`/`next_cursor` 分页拉到底（`MAX_HISTORY_PAGES=50` 做安全阀防 API 异常导致死循环），返回前反转为按 ts 升序（Slack 原生按降序返回）；游标推进移到 for 循环内部，每条消息被标记为"已处理"（含机器人消息、非白名单发件人被跳过的情况）的**同一时刻**才 `_save_last_check_ts`，不再整批处理前一次性写死；查询起点加 10 分钟重叠窗口（`CURSOR_OVERLAP_SECONDS`）防边界时钟误差，重复消息靠既有 `processed_slack_ts.json` 幂等吸收。用 mock 过 `_slack_get` 做了本地烟雾测试验证分页聚合顺序和游标推进语义，未对生产 Slack 频道实跑（避免误触发真实消息处理），下一次 launchd 5 分钟轮询会自然验证。**教训**：和 issue #5/#6 是同一个模式家族的第三次出现——"先写游标/状态标记，后处理"是这类轮询系统最容易踩的坑，本质上是把"我打算处理这些"和"我已经处理完这些"两个不同的时刻错误地合并成了一个写入点；游标/水位线的写入时机必须严格跟在"已确认处理成功"之后，不能提前。

### 24. 无日期转载文章绕过时效过滤，"本周新动态"实为一年前旧事（已修复 2026-07-12，issue #12）
2026-07-12 周报「安踏集团」一节把 FY2025（2025-03-25 披露）全年业绩包装成"本周新动态"——LLM 自己在正文推理里写着"恰逢公司于2025年3月25日发布全年业绩公告之后"，却没把这个日期用于丢弃决策。根因：`fetch_company_raw()` 的 `PUB_DATE_MAX_AGE=9` 天硬过滤只对有可解析 `published_date` 的文章生效；一篇近期被抓取到的英文转载报道（Ecotextile News）没有可解析日期，完全落到 `prefilter_articles()`（deepseek-v4-flash）prompt 第 1 条"若内容仅涉及历史事件则超期丢弃"的自由文本判断上，这次判断不可靠。**修复**：`prefilter_articles()` 的 `keep` 输出 schema 从 `[0,1,2]` 改为 `[{"i": idx, "event_date": "YYYY-MM-DD"|null}]`，强制 LLM 显式抽取"核心事实性事件日期"（区分于文章自身发布/转载时间）；Python 侧新增确定性二次过滤（`EVENT_MAX_AGE_DAYS=30`），不再信任 LLM 的 keep/skip 判断已经应用了它自己提取出的日期，event_date 无法解析/缺失时按原规则保留。用 monkeypatch 模拟"event_date=2025-03-25 但仍被 keep"场景验证过滤生效。**教训**：LLM 在推理文本里已经"知道"正确答案（这里是准确的事件日期），不代表它会把这个认知一致地应用到最终的结构化决策字段上——自由文本判断和结构化输出之间存在推理-决策断层；凡是能从模型输出里拆出一个独立、可机器验证的字段（如日期、数字、布尔），就该拆出来在代码侧做确定性校验，而不是让同一次生成里的"叙述"和"决策"隐式保持一致。

### 25. OpenRouter `HTTP-Referer` 用裸字符串而非 URL，归属 header 静默失效（已修复 2026-07-13）
2026-07-12 引入 `OR_ATTRIBUTION_HEADERS` 常量（`email_check.py`、`run_intel_deepseek_test.py` 共 4 处调用点）时，`HTTP-Referer` 的值写成了裸字符串 `"PhysicalClue611"`，不是合法 URL。按 OpenRouter [app-attribution 文档](https://openrouter.ai/docs/app-attribution)，`HTTP-Referer` 必须能被 URL parser 正常解析（`https://` 开头），否则 OR 会**静默丢弃整个归属**，dashboard/日志里 App 一栏显示空白或 "Unknown"，同一请求里的 `X-OpenRouter-Title` 也连带失效——不报错、不告警，唯一暴露方式是去 dashboard 核对 App 归属是否符合预期。**修复**：`HTTP-Referer` 改为项目 GitHub 仓库地址 `https://github.com/PhysicalClue611/China_Market_Intelligence`（private repo 同样符合格式要求，不需要公网可达）；`X-OpenRouter-Title` 保持 `"MI"` 不变。用 `urllib.parse.urlparse` 验证过新值 `scheme=https`、`netloc` 非空。**教训**：像"App 归属""日志标签"这类旁路 header，一旦格式错误通常不会报错、不会影响主请求成功与否，只是静默丢失一个本该有的元数据——上线新 header 约定时要单独核实其格式符合目标服务的规范，不能只验证"请求没报错"就当作已生效；对没有强校验反馈的输入，格式错误和没配置一样容易被长期忽略。

### 26. `_lookup_english_name` 的 `max_tokens=30` 让推理型 provider 把预算全花在思考上，`content=null` 崩溃（已修复 2026-07-13）
验证 issue #25 归属 header 时手动触发了一次真实调用（`openai/gpt-oss-20b`，provider 落在 `DekaLLM`），返回 `finish_reason: "length"`、`content: null`、`reasoning` 里是一句被截断到一半的思考文本（"...no"）。原因和 PITFALLS.md #1/#11 是同一类：`max_tokens=30` 对这个 provider 而言太小，30 个 token 全部耗在 `reasoning` 字段的思考链上，还没轮到输出最终答案就被截断，调用方 `data["choices"][0]["message"]["content"].strip()` 对 `None` 调 `.strip()` 直接抛 `AttributeError`，被外层 `except Exception` 吞掉，函数返回空字符串——效果上和"未识别英文名"完全一样，日志里也看不出这是 token 预算问题还是模型真的不认识这家公司。**修复**：`max_tokens` 从 30 提到 300（实测同一 provider 在 300 token 预算下 `finish_reason: stop`，`content: "BYD"`，reasoning 只用了 32 token，思考本身很短，纯粹是预算给太紧）；取值方式改为 `(msg.get("content") or "").strip()`，避免 `None.strip()` 崩溃。**没有**照搬 #11 的 `content or reasoning` 兜底——这里的 `reasoning` 是半截思考句子，不是可用的英文名，回退到它会把垃圾文本悄悄写进公司配置，比返回空字符串（触发"未识别英文名，可回复纠正"提示）更危险。**教训**：`max_tokens` 过小导致推理模型 `content=null` 不是一次性修完的问题，是这类 OpenRouter 推理模型/provider 组合的通用风险——新增或修改任何走推理模型的调用点时，都要用真实请求核实 `finish_reason` 是 `stop` 而不是 `length`，不能只看"没报错"就认为 token 预算够用；`content or reasoning` 兜底本身也不是万能模板，要先确认 `reasoning` 字段的内容是不是真的能当作有效输出用。

**后续（2026-07-13，未采纳，仅记录路径）**：用户指出 30/300 这两个数字都是"抄的、不是算出来的"——这个调用极低频（仅在用户邮件里加监控公司时触发一次），成本可忽略（166 token 约 $0.000008），没必要卡着刚好够用的最小 token 数。查过 OpenRouter 文档（[reasoning-tokens](https://openrouter.ai/docs/use-cases/reasoning-tokens)）并做过真实调用验证：可以传 `"reasoning": {"effort": "minimal"}` 主动压低这个模型的思考量，而不是被动加大 `max_tokens` 硬扛。实测对比（同一 prompt，`provider` 落在 `Amazon Bedrock`）：
- 不传 `reasoning` 参数：reasoning_tokens ≈ 32-38
- `reasoning: {"effort": "minimal"}`：reasoning_tokens = 9，`finish_reason: stop`，`content: "BYD"` 正确
- `reasoning: {"effort": "none"}`：该模型不支持，返回 `HTTP 400`
- `reasoning: {"exclude": true}`：只是不把思考文本包含在响应里，实际计算/token 消耗不变（38 token），不省钱不省延迟，价值有限

当前 `max_tokens=300` 已经稳定工作（见本条修复），用户决定不动它——现状够用就不追求"更彻底"。但如果这个函数将来又复现同样的 `content=null`/`finish_reason=length` 症状（比如换了个思考更啰嗦的 provider），直接加 `"reasoning": {"effort": "minimal"}`，比继续加大 `max_tokens` 更对症：前者从根源减少思考 token 消耗，后者只是给更大的缓冲垫，不解决"预算被思考占用"这件事本身。

### 27. 三处"先标记已处理，后执行有风险操作"+ 两个入口缺失关键 env 仍 exit 0（已修复 2026-07-15，issue #8/#9/#11）
issue #5/#6/#7（踩坑 #20-23）修过的两类模式在其余入口原样存在，只是换了个地方复现：

**(a) 先写水位线/处理标记，后执行会失败的操作**（issue #8、#9，与 #23 同族）：
- `slack_check.py` 对有效追问（thread 回复 / `mi:` 前缀）：`_save_processed_ts`/`_save_last_check_ts` 写在 `_followup_three_stage()` 和 `post_message()` **之前**，pipeline 崩溃或 `chat.postMessage` 失败（token 过期/限流/权限）只返回 `None`、调用方不检查，追问永久丢失、不会重试。
- `email_check.py` 的 `run_email_check()` 对每封授权邮件：`_save_processed_id(email_id)` + `_advance_cursor(email)` 在回复邮件发送之后**无条件**执行，`send_report()`（Resend 故障/限流/密钥失效）失败只返回 `None` 不抛异常，调用方不检查，指令已在服务端执行（如 `add_company`）但用户永远收不到回复，且不会重试。

**(b) 关键 env 缺失仍静默 return，exit 0**（issue #11，与 #22 同族）：`run_intel.py`（`TAVILY_API_KEY`/`DEEPSEEK_API_KEY`）、`slack_check.py`（`SLACK_BOT_TOKEN`/`SLACK_MI_CHANNEL`）在缺失时只 `logger.error`/`logger.info` 后 `return`，launchd 判定"成功"，只有 #22 修过的 `email_check.py` 有 fail-fast。

**修复**：
- `run_intel.py` 新增 `_validate_intel_config()`（`TAVILY_API_KEY`/`DEEPSEEK_API_KEY`/`HERMES_DATA`/`OBSIDIAN_PATH`），`slack_check.py` 新增 `_validate_slack_config()`（`SLACK_BOT_TOKEN`/`SLACK_MI_CHANNEL`），均在入口函数最开头调用，缺失即抛专用异常（`IntelConfigError`/`SlackConfigError`），逃逸到既有 `__main__` try/except/raise 触发非零 exit——与 `email_check.py` 的 `JMAPConfigError` 同一模式。
- `slack_check.py`：有效追问改为先跑 `_followup_three_stage` + `post_message`，仅当 `post_message` 返回非空 ts 后才写 processed/cursor；`post_message` 失败只记 ERROR，留给下一轮重叠窗口重试。无 question 的路径（非白名单、机器人消息、thread 父消息非 bot）无业务副作用，continue 前立即标记不受影响。
- `email_check.py`：`_send_result_email()` 改为返回 bool；同步/异步（ack）两条路径均只在发送成功后才 `_save_processed_id(email_id)` + `_advance_cursor`。额外加了 `cursor_stalled` 标志——同一批次里，若某封邮件回复失败，即使批次内更晚的邮件回复成功，也不再推进游标，防止游标跳过失败邮件导致其永久无法被重叠窗口捕获重试（未授权发件人的跳过路径无需回复，标记不受 `cursor_stalled` 影响，但游标推进仍受其约束）。

**教训**：这是同一个模式家族第 4/5 次出现（#20/#22 是"env 缺失"，#23 是"先写游标后处理"）——修 bug 时只堵住触发它的那一个入口，不代表同一代码库里结构相同的其他入口也修了；新入口/新轮询循环一律要检查"标记完成"和"业务副作用是否已确认成功"这两个时间点有没有被错误合并，以及关键配置缺失是不是走了 fail-fast 而不是静默降级。
