from __future__ import annotations
import os
from pathlib import Path

APP_TITLE = "CRM NPD Embalagens"
DB_FILENAME = "crm_npd.db"
SECRET_KEY = os.environ.get("CRM_NPD_SECRET", "crm_npd_secret_key_change_me")
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# CRM_NPD_DB_PATH permite apontar a aplicacao para outro banco sem tocar no
# codigo. E o que deixa a captura golden-master rodar sobre uma copia congelada,
# em vez de escrever no crm_npd.db de producao.
_db_override = os.environ.get("CRM_NPD_DB_PATH", "").strip()
DB_PATH = Path(_db_override) if _db_override else PROJECT_ROOT / DB_FILENAME

CACHE_TTL_SECONDS = int(os.environ.get("CRM_NPD_CACHE_TTL", "45"))
SLOW_REQUEST_MS = int(os.environ.get("CRM_NPD_SLOW_REQUEST_MS", "900"))
