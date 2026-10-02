"""Configuracao tipada lida de variaveis de ambiente.

Segredos nunca ficam no codigo. Copie ``.env.example`` para ``.env``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Parametros de execucao do CRM NPD."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    crm_npd_secret: str = "crm_npd_secret_key_change_me"
    crm_npd_db_path: str = ""
    crm_npd_cache_ttl: int = 45
    crm_npd_log_level: str = "INFO"
    crm_npd_fonte: str = "sqlite"
    sisplan_host: str = "192.168.100.168"
    sisplan_port: int = 3051
    sisplan_database: str = r"C:\Sisplan\Base\14215F.FDB"
    sisplan_user: str = "SYSDBA"
    sisplan_password: str = ""
    sisplan_fbclient: str = r"C:\dev\auto_sisplan\fbclient_x64.dll"


def get_settings() -> Settings:
    """Carrega as settings uma vez por processo."""
    return Settings()
