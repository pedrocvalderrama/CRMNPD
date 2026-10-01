from __future__ import annotations
import os
from pathlib import Path

APP_TITLE = "CRM NPD Embalagens"
DB_FILENAME = "crm_npd.db"
SECRET_KEY = os.environ.get("CRM_NPD_SECRET", "crm_npd_secret_key_change_me")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / DB_FILENAME
CACHE_TTL_SECONDS = int(os.environ.get("CRM_NPD_CACHE_TTL", "45"))
SLOW_REQUEST_MS = int(os.environ.get("CRM_NPD_SLOW_REQUEST_MS", "900"))
