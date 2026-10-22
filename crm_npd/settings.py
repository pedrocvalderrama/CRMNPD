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
    crm_npd_host: str = "0.0.0.0"
    crm_npd_port: int = 8089
    crm_npd_db_path: str = ""
    crm_npd_cache_ttl: int = 45
    crm_npd_log_level: str = "INFO"
    crm_npd_fonte: str = "firebird"
    sisplan_host: str = "192.168.100.168"
    sisplan_port: int = 3051
    sisplan_database: str = r"C:\Sisplan\Base\14215F.FDB"
    sisplan_user: str = "SYSDBA"
    sisplan_password: str = ""
    sisplan_charset: str = "WIN1252"
    sisplan_fbclient: str = ""
    sisplan_cdc_interval: int = 5
    sisplan_cdc_batch: int = 2000
    sisplan_cdc_auto: str = "1"
    sisplan_sync_on_start: str = "stale"


def get_settings() -> Settings:
    """Carrega as settings uma vez por processo."""
    return Settings()


def aplicar_env() -> Settings:
    """Copia o ``.env`` para ``os.environ`` (conexao Firebird le o ambiente).

    Nao sobrescreve variavel ja definida no processo. Padrao = Sisplan da
    fabrica (MANUAL_ACESSO_BANCO_SISPLAN.md). Override local: SISPLAN_HOST=local.
    """
    import os

    cfg = get_settings()
    pares = {
        "CRM_NPD_SECRET": cfg.crm_npd_secret,
        "CRM_NPD_HOST": cfg.crm_npd_host,
        "CRM_NPD_PORT": str(cfg.crm_npd_port),
        "CRM_NPD_DB_PATH": cfg.crm_npd_db_path,
        "CRM_NPD_CACHE_TTL": str(cfg.crm_npd_cache_ttl),
        "CRM_NPD_LOG_LEVEL": cfg.crm_npd_log_level,
        "CRM_NPD_FONTE": cfg.crm_npd_fonte,
        "SISPLAN_HOST": cfg.sisplan_host,
        "SISPLAN_PORT": str(cfg.sisplan_port),
        "SISPLAN_DATABASE": cfg.sisplan_database,
        "SISPLAN_USER": cfg.sisplan_user,
        "SISPLAN_PASSWORD": cfg.sisplan_password,
        "SISPLAN_CHARSET": cfg.sisplan_charset,
        "SISPLAN_FBCLIENT": cfg.sisplan_fbclient,
        "SISPLAN_CDC_INTERVAL": str(cfg.sisplan_cdc_interval),
        "SISPLAN_CDC_BATCH": str(cfg.sisplan_cdc_batch),
        "SISPLAN_CDC_AUTO": cfg.sisplan_cdc_auto,
        "SISPLAN_SYNC_ON_START": cfg.sisplan_sync_on_start,
    }
    for chave, valor in pares.items():
        if valor and chave not in os.environ:
            os.environ[chave] = str(valor)
    if "PORT" not in os.environ:
        os.environ["PORT"] = str(cfg.crm_npd_port)
    return cfg
