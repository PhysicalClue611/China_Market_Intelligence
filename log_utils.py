import logging
import logging.handlers
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
RETENTION_DAYS = 30


def setup_logging(name: str, fmt: str = "%(asctime)s %(levelname)s %(message)s"):
    """Configure logging to a self-rotating file under the project's own logs/
    directory (not /tmp, which macOS periodically sweeps), plus stdout."""
    LOG_DIR.mkdir(exist_ok=True)
    _purge_old_logs()

    formatter = logging.Formatter(fmt)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_DIR / f"{name}.log", when="midnight", backupCount=RETENTION_DAYS, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])


def _purge_old_logs(days: int = RETENTION_DAYS):
    cutoff = datetime.now().timestamp() - days * 86400
    for f in LOG_DIR.glob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
