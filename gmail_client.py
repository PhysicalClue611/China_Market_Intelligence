# DEPRECATED: replaced by Resend (send) + Stalwart JMAP (receive). No longer used by production scripts.
import logging
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("HERMES_DATA", "/opt/data"))
TOKEN_PATH = DATA_DIR / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def get_service():
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(f"token.json not found at {TOKEN_PATH}")
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
        logger.info("Gmail token refreshed")
    return build("gmail", "v1", credentials=creds)
