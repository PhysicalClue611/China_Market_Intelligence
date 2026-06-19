import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("HERMES_DATA", "/opt/data"))
OBSIDIAN_DIR = Path(os.getenv("OBSIDIAN_PATH", "/opt/obsidian"))
CONFIG_PATH = DATA_DIR / "intel_config.yaml"
WATCHLIST_PATH = OBSIDIAN_DIR / "Hermes" / "MI" / "watchlist.md"


# ── watchlist.md parser ───────────────────────────────────────────────────────

def _parse_watchlist(text: str) -> dict | None:
    """Parse Obsidian watchlist.md into {companies: [...], recipients: [...]}."""
    companies, recipients = [], []
    section = None
    lines = text.splitlines()
    # Skip YAML frontmatter (--- ... ---)
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break
    for raw_line in lines[start:]:
        line = raw_line.strip()
        if not line:
            continue
        if line == "## companies":
            section = "companies"
        elif line == "## recipients":
            section = "recipients"
        elif line.startswith("##"):
            section = None
        elif line.startswith("#"):  # comment line, skip
            continue
        elif section == "companies":
            parts = [p.strip() for p in line.split("|", 1)]
            zh = parts[0]
            en = parts[1] if len(parts) > 1 else ""
            if zh:
                companies.append({"zh": zh, "en": en})
        elif section == "recipients":
            if "@" in line:
                recipients.append(line)
    if not companies and not recipients:
        return None
    return {"companies": companies, "recipients": recipients}


def _write_watchlist(config: dict):
    """Write managed sections back to watchlist.md, preserving frontmatter and other content."""
    existing = WATCHLIST_PATH.read_text(encoding="utf-8") if WATCHLIST_PATH.exists() else ""
    existing_lines = existing.splitlines()

    companies_block = ["## companies", "# 格式：中文名 | English Name（可空）"]
    for c in config.get("companies", []):
        zh = c.get("zh", "")
        en = c.get("en", "")
        companies_block.append(f"{zh} | {en}" if en else zh)

    recipients_block = ["## recipients"]
    for r in config.get("recipients", []):
        recipients_block.append(r)

    # Preserve lines before ## companies and after the recipients block.
    # Use a four-state machine to skip the entire managed zone including recipient entries.
    pre, post = [], []
    state = "pre"  # pre | companies | recipients | post
    for line in existing_lines:
        stripped = line.strip()
        if state == "pre":
            if stripped.startswith("## companies"):
                state = "companies"
            else:
                pre.append(line)
        elif state == "companies":
            if stripped.startswith("## recipients"):
                state = "recipients"
            # else: skip managed content
        elif state == "recipients":
            if stripped.startswith("##") and not stripped.startswith("## recipients"):
                state = "post"
                post.append(line)
            # else: skip old recipient entries and blank lines
        elif state == "post":
            post.append(line)

    result = pre + companies_block + [""] + recipients_block + [""] + post
    WATCHLIST_PATH.write_text("\n".join(result), encoding="utf-8")


# ── yaml config (fallback / sync target) ─────────────────────────────────────

def _normalize_companies(companies: list) -> list:
    """Migrate old string format to {zh, en} objects."""
    result = []
    for c in companies:
        if isinstance(c, str):
            result.append({"zh": c, "en": ""})
        elif isinstance(c, dict):
            result.append({"zh": c.get("zh", ""), "en": c.get("en", "")})
    return result


def _load_yaml() -> dict:
    if not CONFIG_PATH.exists():
        default = {"companies": [], "recipients": []}
        _save_yaml(default)
        return default
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    raw = config.get("companies", [])
    normalized = _normalize_companies(raw)
    if normalized != raw:
        config["companies"] = normalized
        _save_yaml(config)
    return config


def _save_yaml(config: dict):
    CONFIG_PATH.write_text(
        yaml.dump(config, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


# ── Public API ────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load config: prefer watchlist.md, fallback to intel_config.yaml."""
    try:
        if WATCHLIST_PATH.exists():
            parsed = _parse_watchlist(WATCHLIST_PATH.read_text(encoding="utf-8"))
            if parsed:
                return parsed
    except Exception as e:
        logger.warning(f"watchlist.md read failed, falling back to yaml: {e}")
    return _load_yaml()


def save_config(config: dict):
    """Write to both watchlist.md and intel_config.yaml to keep them in sync."""
    try:
        WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        _write_watchlist(config)
    except Exception as e:
        logger.warning(f"watchlist.md write failed: {e}")
    _save_yaml(config)


def get_companies() -> list[str]:
    """Return list of zh names (for display and backward compat)."""
    return [c["zh"] for c in load_config().get("companies", [])]


def get_companies_full() -> list[dict]:
    """Return list of {zh, en} dicts for query construction."""
    return load_config().get("companies", [])


def get_recipients() -> list:
    return load_config().get("recipients", [])


def add_company(zh: str, en: str = "") -> bool:
    config = load_config()
    companies = config.setdefault("companies", [])
    if zh not in [c["zh"] for c in companies]:
        companies.append({"zh": zh, "en": en})
        save_config(config)
        return True
    return False


def remove_company(zh: str) -> bool:
    config = load_config()
    companies = config.setdefault("companies", [])
    before = len(companies)
    config["companies"] = [c for c in companies if c["zh"] != zh]
    if len(config["companies"]) < before:
        save_config(config)
        return True
    return False


def add_recipient(email: str) -> bool:
    config = load_config()
    if email not in config.setdefault("recipients", []):
        config["recipients"].append(email)
        save_config(config)
        return True
    return False


def remove_recipient(email: str) -> bool:
    config = load_config()
    recipients = config.setdefault("recipients", [])
    if email in recipients:
        recipients.remove(email)
        save_config(config)
        return True
    return False
