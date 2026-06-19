# MI — Market Intelligence 自动化系统

这是 `~/MI` 的项目记忆文件。在这个目录下启动的 Codex 会话会自动读取它。

## 当前系统状态（2026-05-04）

**已从 Hermes 容器完全迁出，宿主机原生运行。** 首次 dry run 2026-05-04 验证通过：6/6 公司成功，$0.0031/次，邮件 + Telegram 正常。

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
    ├── credentials.json  # Gmail OAuth client secret（重新授权时用）
    ├── token.json        # Gmail OAuth token（运行时自动刷新）
    ├── article_cache.json
    ├── seen_urls.json
    ├── fetch_log.json
    ├── processed_email_ids.json
    └── intel_config.yaml # 公司列表 fallback（主配置在 Obsidian watchlist.md）
```

---

## 调度（宿主机 launchd）

| plist | 周期 | 脚本 | 日志 |
|---|---|---|---|
| `com.hermes.intel` | 周日 08:59 PDT | `~/MI/run_intel.py` | `/tmp/hermes_intel.log` |
| `com.hermes.emailcheck` | 每 5 分钟 | `~/MI/email_check.py` | `/tmp/hermes_emailcheck.log` |

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
| `HERMES_DATA` | `~/MI/data`（脚本数据目录） |
| `OBSIDIAN_PATH` | Obsidian vault 绝对路径 |

---

## 脚本说明

| 脚本 | 用途 |
|---|---|
| `run_intel.py` | 情报主逻辑：搜索 → 去重 → LLM 分析 → Obsidian + 邮件 |
| `email_check.py` | 邮件轮询：解析指令 → 三段式 followup pipeline |
| `search_utils.py` | 三级搜索 fallback：Tavily → SerpApi → Serper |
| `config_store.py` | 公司/收件人配置，优先读 Obsidian watchlist.md |
| `dedup_utils.py` | L2 Jaccard + L3 MemPalace 去重 |
| `article_cache.py` | 文章全文缓存（90天TTL） |
| `gmail_client.py` | Gmail API OAuth2 封装 |
| `email_sender.py` | 邮件发送，返回 sent_id + inbox_id |
| `http_utils.py` | httpx 重试封装（网络错误/5xx 重试，4xx 不重试） |
| `memory_context.py` | 每公司 LLM 前注入历史上下文（bridge REST）：MemPalace 语义搜索 + Obsidian 全文搜索 |

---

## API 路由策略

| 场景 | 模型 |
|---|---|
| 情报文本分析 | `deepseek/deepseek-v4-flash`（via OpenRouter） |
| 邮件指令解析 / 英文名推断 | `openai/gpt-oss-20b` |
| 情报抓取 | Tavily `topic=general, days=30` |

OpenRouter provider 白名单：`["Inceptron", "AkashML", "Nebius", "NovitaAI", "Parasail"]`

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

### 1. Prefilter 返回空 JSON（pass-through 降级）
DeepSeek V4 Flash 在 prefilter 阶段偶尔返回空响应（`Expecting value: line 1 column 1`），原因是推理模型在 reasoning 阶段消耗完 token。已有 pass-through 降级逻辑，不影响主流程，但 prefilter 质量丢失。2026-05-04 dry run 中 4/6 家触发，正常。

### 2. load_dotenv 路径层级
脚本从 `~/.hermes/skills/intel/` 搬至 `~/MI/` 后，`load_dotenv` 的相对路径少一级 `..`。已修复为 `os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")`。

### 3. bridge URL 在容器内外不同
容器内用 `host.lima.internal:8765`，宿主机原生运行用 `localhost:8765`。迁出时已全部改为 `localhost:8765`（`dedup_utils.py`、`memory_context.py`）。

---

## 去重架构

| 层 | 机制 | 状态 |
|---|---|---|
| L1 URL | `seen_urls.json` 90天TTL | 运行中 |
| L2 标题 Jaccard | `article_cache.json`，阈值 0.45 | 运行中 |
| L2.5 V4 Flash 预处理门控 | 时效/相关性/信息量过滤 | 运行中 |
| L3 MemPalace 语义 | 待积累 2-3 月数据后启用 | 未启用 |
