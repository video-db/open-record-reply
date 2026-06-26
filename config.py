"""Configuration constants and environment loading."""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

COLLECTION_NAME = "mcp-record-replay"
SKILLS_ROOT = Path.home() / ".mcp-videodb" / "skills"
SESSIONS_ROOT = Path.home() / ".mcp-videodb" / "sessions"
SKILLS_ROOT.mkdir(parents=True, exist_ok=True)
SESSIONS_ROOT.mkdir(parents=True, exist_ok=True)

CAPTURE_ACTIVE_TIMEOUT_SECONDS = 30
EXPORT_TIMEOUT_SECONDS = 300
EXPORT_POLL_INTERVAL_SECONDS = 1

SCENE_INDEX_TIME_INTERVAL = 3
SCENE_INDEX_FRAME_COUNT = 3
LLM_MODEL = "pro"
LLM_MAX_RETRIES = 2

AX_SEND_TIMEOUT = 15.0
AX_EXECUTE_TIMEOUT = 30.0

API_KEY = os.environ.get("VIDEODB_API_KEY", os.environ.get("VIDEO_DB_API_KEY", ""))
BASE_URL = os.environ.get("VIDEO_DB_DEV_URL", os.environ.get("VIDEODB_BASE_URL", ""))
if not API_KEY:
    raise RuntimeError("VIDEODB_API_KEY not set in environment or .env file")
