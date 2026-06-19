# China Market Intelligence (MI)

A weekly automation pipeline that researches Chinese public companies, synthesizes findings with a reasoning LLM, and delivers structured intelligence reports to Obsidian, email, and Slack.

## What it does

Every Sunday the pipeline:

1. **Searches** recent news for each monitored company across three providers (Tavily → SerpApi → Serper), supplemented by Chinese-language news via Serper News
2. **Deduplicates** across four layers: URL cache, title Jaccard similarity, LLM-based relevance/freshness gate, and optional MemPalace semantic similarity
3. **Enriches** each company with historical context pulled from past reports (MemPalace vector search + Obsidian full-text search)
4. **Synthesizes** a structured report per company using DeepSeek V4 Pro with extended reasoning
5. **Delivers** the combined report to an Obsidian vault note, an HTML email via Resend, and a Slack channel

An inbound email listener (`email_check.py`) and Slack listener (`slack_check.py`) accept follow-up queries and run ad-hoc research pipelines on demand.

## Architecture

```
Search (Tavily / SerpApi / Serper)
        |
  Multi-layer dedup
  L1 URL cache · L2 Jaccard · L2.5 LLM gate
        |
  Context injection (MemPalace + Obsidian, optional)
        |
  DeepSeek V4 Pro synthesis
        |
  ┌─────────────────────────┐
  │  Obsidian vault note    │
  │  Email (Resend API)     │
  │  Slack channel post     │
  └─────────────────────────┘
```

## Prerequisites

### Runtime

| Requirement | Notes |
|---|---|
| Python 3.11+ | Managed via `uv` |
| [uv](https://github.com/astral-sh/uv) | `pip install uv` or `brew install uv` |
| macOS (for scheduling) | Scripts run on any platform manually; launchd scheduling is macOS-only |
| [Obsidian](https://obsidian.md) | Output vault and watchlist configuration |

### API Keys

All keys go in a `.env` file at the project root (copy `.env.example` as a starting point).

**Required — core pipeline:**

| Variable | Service | Purpose |
|---|---|---|
| `DEEPSEEK_API_KEY` | [DeepSeek Platform](https://platform.deepseek.com) | Primary LLM for synthesis and prefilter |
| `TAVILY_API_KEY` | [Tavily](https://tavily.com) | Primary news search (10 req/day on free tier) |
| `RESEND_API_KEY` | [Resend](https://resend.com) | Outbound email delivery |
| `FROM_ADDRESS` | — | Sender address for outbound email (e.g. `mi@yourdomain.com`) |
| `HERMES_DATA` | — | Absolute path to the `data/` directory |
| `OBSIDIAN_PATH` | — | Absolute path to your Obsidian vault root |

**Required — inbound email (email_check.py):**

| Variable | Notes |
|---|---|
| `STALWART_API_KEY` | Bearer token for your [Stalwart](https://stalw.art) mail server's JMAP API |
| `JMAP_BASE` | Base URL of the JMAP endpoint, e.g. `https://mail.yourdomain.com:8443` |
| `JMAP_ACCOUNT_ID` | JMAP account ID (find via `GET /jmap/` session) |
| `JMAP_INBOX_ID` | JMAP mailbox ID of INBOX |

**Required — Slack:**

| Variable | Notes |
|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-...` token; needs `chat:write`, `channels:history` scopes |
| `SLACK_MI_CHANNEL` | Channel ID (not name) where reports are posted |
| `SLACK_ALLOWED_USERS` | Comma-separated Slack user IDs allowed to send follow-up queries |

**Optional — increases search coverage and dedup quality:**

| Variable | Service | Notes |
|---|---|---|
| `SERPAPI_API_KEY` | [SerpApi](https://serpapi.com) | Fallback when Tavily quota is exhausted |
| `SERPER_API_KEY` | [Serper.dev](https://serper.dev) | Third-level fallback + Chinese news supplement |
| `JINA_API_KEY` | [Jina AI](https://jina.ai) | Full-text article extraction (Reader API) |
| `OPENROUTER_API_KEY` | [OpenRouter](https://openrouter.ai) | Alternative LLM routing |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API | Status/error notifications |
| `TELEGRAM_ALLOWED_USERS` | — | Telegram user IDs for notifications |
| `GITHUB_TOKEN` | GitHub | Issue/PR automation |

### Infrastructure (optional)

`memory_context.py` calls a local bridge server at `http://localhost:8765` for MemPalace vector search and Obsidian full-text search. If the bridge is not running, context injection silently fails open — the pipeline still runs without historical context. Setting this up requires a separate MemPalace/Obsidian bridge service (not included in this repo).

## Installation

```bash
git clone https://github.com/PhysicalClue611/China_Market_Intelligence.git
cd China_Market_Intelligence

# Install dependencies
uv sync

# Copy and fill in environment variables
cp .env.example .env
# Edit .env with your keys and paths
```

## Configuration

### Company watchlist

The pipeline reads the monitored company list from two sources in priority order:

1. **Obsidian `watchlist.md`** (primary) — located at `$OBSIDIAN_PATH/Hermes/MI/watchlist.md`:

```markdown
## companies
# Format: 中文名 | English Name
海尔集团 | Haier Group
比亚迪 | BYD

## recipients
you@example.com
```

2. **`data/intel_config.yaml`** (fallback) — used when `watchlist.md` is absent or empty.

### Scheduling (macOS launchd)

Example plist for weekly Sunday runs at 08:59 local time:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.mi.intel</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/MI/.venv/bin/python</string>
    <string>/path/to/MI/run_intel.py</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>0</integer>
    <key>Hour</key><integer>8</integer>
    <key>Minute</key><integer>59</integer>
  </dict>
  <key>StandardOutPath</key><string>/tmp/mi_intel.log</string>
  <key>StandardErrorPath</key><string>/tmp/mi_intel.log</string>
</dict>
</plist>
```

Place in `~/Library/LaunchAgents/` and load with `launchctl load`.

## Running manually

```bash
# Full weekly run
.venv/bin/python run_intel.py

# Skip deduplication (force re-process all companies)
.venv/bin/python run_intel.py --force

# Inbound email listener (normally run every 5 minutes via launchd)
.venv/bin/python email_check.py

# Slack listener
.venv/bin/python slack_check.py
```

## Output

Reports are written to your Obsidian vault at:

```
$OBSIDIAN_PATH/Hermes/MI/YYYY-MM-DD-china-companies.md
```

The same content is delivered as an HTML email and posted to the configured Slack channel.

## Scripts

| Script | Purpose |
|---|---|
| `run_intel.py` | Main pipeline: search → dedup → synthesize → deliver |
| `email_check.py` | Polls JMAP inbox, parses commands, runs follow-up pipelines |
| `slack_check.py` | Polls Slack channel for follow-up queries |
| `search_utils.py` | Three-tier search: Tavily → SerpApi → Serper |
| `dedup_utils.py` | L1 URL + L2 Jaccard deduplication |
| `article_cache.py` | 90-day article full-text cache |
| `memory_context.py` | Historical context injection via bridge API |
| `config_store.py` | Company/recipient config (watchlist.md + YAML fallback) |
| `email_sender.py` | Outbound email via Resend API |
| `slack_sender.py` | Slack delivery with Markdown conversion |
| `http_utils.py` | httpx wrapper with retry (network errors / 5xx) |
| `hermes_footer.py` | Report footer generation |

## Contributing

Bug reports and feature requests go through [GitHub Issues](https://github.com/PhysicalClue611/China_Market_Intelligence/issues). Please open an issue before submitting a pull request.

## License

MIT
