"""Sincronizacao incremental por Timestamp Log da planilha Google Sheets `notas_ml`.

Consome o endpoint Apps Script (`?action=changes&since_ts=...`) documentado em
`MANUAL_LEITURA_PLANILHA_NOTAS_ML.md`, funcionando como um CDC leve:
- Quando nada mudou na planilha, recebe `has_changes: false` sem ler linhas.
- Quando entram notas novas, grava apenas o delta em `mercado_livre_invoices`
  e so avanca o cursor `ml_sheets_last_ts` apos o `commit` no SQLite.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

from crm_npd.services.mercado_livre import (
    aplicar_lote_notas_ml,
    ensure_mercado_livre_table,
    normalizar_nota_sheets_ml,
)

logger = logging.getLogger(__name__)

DEFAULT_ML_WEBHOOK_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbxqYYYGWO8oIy9GULK5EvMH6GIeNjIQLw8NEtflS0gm__rmTTXNwhPeVtJtjGS59eifRA/exec"
)

CHAVE_ML_WEBHOOK_URL = "ml_sheets_webhook_url"
CHAVE_ML_LAST_TS = "ml_sheets_last_ts"
CHAVE_ML_LAST_ROW = "ml_sheets_last_row"
CHAVE_ML_LAST_CHECK = "ml_sheets_last_check_at"
CHAVE_ML_LAST_SYNC = "ml_sheets_last_sync_at"
CHAVE_ML_LAST_STATUS = "ml_sheets_last_status"
CHAVE_ML_TOTAL_SHEET = "ml_sheets_total_in_sheet"

_ml_thread: threading.Thread | None = None
_ml_parar = threading.Event()
_ml_thread_lock = threading.Lock()
_ml_exec_lock = threading.Lock()


def _ensure_settings_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )


def _get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if not row or row[0] is None:
        return default
    return str(row[0])


def _set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def intervalo_sync_ml() -> float:
    """Retorna o intervalo em segundos entre leituras do Timestamp Log (padrao 60s)."""
    bruto = os.environ.get("ML_SHEETS_SYNC_INTERVAL", "60").strip() or "60"
    try:
        return max(15.0, float(bruto))
    except ValueError:
        return 60.0


def sync_ml_auto_ativo() -> bool:
    """Indica se o listener automatico da planilha `notas_ml` esta habilitado."""
    bruto = os.environ.get("ML_SHEETS_SYNC_AUTO", "1").strip().lower()
    return bruto not in ("0", "false", "nao", "no", "off", "never", "nunca")


def sync_ml_on_start_ativo() -> bool:
    """Indica se a verificacao inicial na subida do sistema esta habilitada."""
    bruto = os.environ.get("ML_SHEETS_SYNC_ON_START", "1").strip().lower()
    return bruto not in ("0", "false", "nao", "no", "off", "never", "nunca")


def resolver_webhook_url_ml(conn: sqlite3.Connection | None = None) -> str:
    """Resolve a URL do Apps Script priorizando SQLite `settings`, `.env` e padrao."""
    if conn is not None:
        _ensure_settings_table(conn)
        salvo = _get_setting(conn, CHAVE_ML_WEBHOOK_URL, "").strip()
        if salvo:
            return salvo
    env_url = os.environ.get("ML_SHEETS_WEBHOOK_URL", "").strip()
    return env_url or DEFAULT_ML_WEBHOOK_URL


def obter_status_sync_ml(conn: sqlite3.Connection) -> dict[str, Any]:
    """Devolve o estado atual do cursor e da sincronizacao com `notas_ml`."""
    _ensure_settings_table(conn)
    ensure_mercado_livre_table(conn)
    total_banco_row = conn.execute("SELECT COUNT(*) FROM mercado_livre_invoices").fetchone()
    total_banco = int(total_banco_row[0] or 0) if total_banco_row else 0
    last_row_str = _get_setting(conn, CHAVE_ML_LAST_ROW, "0").strip()
    total_sheet_str = _get_setting(conn, CHAVE_ML_TOTAL_SHEET, "0").strip()
    try:
        last_row = int(last_row_str or 0)
    except ValueError:
        last_row = 0
    try:
        total_sheet = int(total_sheet_str or 0)
    except ValueError:
        total_sheet = 0

    return {
        "webhook_url": resolver_webhook_url_ml(conn),
        "last_ts": _get_setting(conn, CHAVE_ML_LAST_TS, "").strip(),
        "last_row": last_row,
        "last_check_at": _get_setting(conn, CHAVE_ML_LAST_CHECK, "").strip(),
        "last_sync_at": _get_setting(conn, CHAVE_ML_LAST_SYNC, "").strip(),
        "last_status": _get_setting(conn, CHAVE_ML_LAST_STATUS, "").strip(),
        "total_in_sheet": total_sheet,
        "total_no_banco": total_banco,
        "auto_ativo": sync_ml_auto_ativo(),
        "thread_viva": bool(_ml_thread is not None and _ml_thread.is_alive()),
        "intervalo_s": int(intervalo_sync_ml()),
    }


def consultar_webhook_ml(
    webhook_url: str,
    since_ts: str = "",
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Executa `GET ?action=changes&since_ts=...` no Apps Script da planilha `notas_ml`."""
    url_limpa = (webhook_url or "").strip()
    if not url_limpa:
        raise ValueError("URL do webhook da planilha Mercado Livre nao configurada.")

    query = urllib.parse.urlencode({"action": "changes", "since_ts": since_ts or ""})
    sep = "&" if "?" in url_limpa else "?"
    full_url = f"{url_limpa}{sep}{query}"

    req = urllib.request.Request(
        full_url,
        headers={
            "Accept": "application/json",
            "User-Agent": "CRM-NPD-ML-Sync/5.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        corpo = resp.read().decode("utf-8", errors="replace")

    try:
        dados = json.loads(corpo)
    except json.JSONDecodeError as exc:
        trecho = corpo[:180].replace("\n", " ").strip()
        raise RuntimeError(f"Resposta invalida do webhook ML (nao e JSON): {trecho}") from exc

    if not isinstance(dados, dict) or not dados.get("success"):
        raise RuntimeError(f"Erro reportado pelo webhook da planilha ML: {dados}")

    return dados


def sincronizar_planilha_ml_delta(
    conn: sqlite3.Connection,
    *,
    forcar_completo: bool = False,
    fetcher: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Consulta apenas notas novas apos `ml_sheets_last_ts` e grava no SQLite.

    O cursor `ml_sheets_last_ts` so avanca na mesma transacao em que as notas
    forem gravadas com sucesso.
    """
    with _ml_exec_lock:
        _ensure_settings_table(conn)
        ensure_mercado_livre_table(conn)

        webhook_url = resolver_webhook_url_ml(conn)
        cursor_ts = "" if forcar_completo else _get_setting(conn, CHAVE_ML_LAST_TS, "").strip()
        cursor_row_str = _get_setting(conn, CHAVE_ML_LAST_ROW, "0").strip()
        try:
            cursor_row = 0 if forcar_completo else int(cursor_row_str or 0)
        except ValueError:
            cursor_row = 0

        agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fn_fetch = fetcher or (lambda u, ts: consultar_webhook_ml(u, ts))

        try:
            dados = fn_fetch(webhook_url, cursor_ts)
            brutas = dados.get("invoices") or []
            if not isinstance(brutas, list):
                brutas = []

            normalizadas: list[dict[str, Any]] = []
            ts_vistos: list[str] = []
            for item in brutas:
                if isinstance(item, dict):
                    ts_item = str(item.get("timestamp_log") or "").strip()
                    if ts_item:
                        ts_vistos.append(ts_item)
                    reg = normalizar_nota_sheets_ml(item)
                    if reg is not None:
                        normalizadas.append(reg)

            res_lote = {
                "registros": 0,
                "novos": 0,
                "substituidos": 0,
                "removidos": 0,
                "duplicados_arquivo": 0,
                "min_data": None,
                "max_data": None,
                "valor_total": 0.0,
            }
            if normalizadas:
                res_lote = aplicar_lote_notas_ml(
                    conn,
                    normalizadas,
                    agora_str,
                    import_id=None,
                    commit=False,
                )

            novo_ts = str(dados.get("last_ts") or "").strip()
            if not novo_ts and ts_vistos:
                novo_ts = max(ts_vistos)
            if not novo_ts:
                novo_ts = cursor_ts

            try:
                nova_row = int(dados.get("last_row") or cursor_row or 0)
            except (TypeError, ValueError):
                nova_row = cursor_row

            total_sheet = dados.get("total_notes_in_sheet")
            if total_sheet is not None:
                try:
                    _set_setting(conn, CHAVE_ML_TOTAL_SHEET, str(int(total_sheet)))
                except (TypeError, ValueError):
                    pass

            teve_mudanca_banco = bool(
                res_lote["novos"] or res_lote["substituidos"] or res_lote["removidos"]
            )
            if brutas or (novo_ts and novo_ts != cursor_ts) or forcar_completo:
                _set_setting(conn, CHAVE_ML_LAST_TS, novo_ts)
                _set_setting(conn, CHAVE_ML_LAST_ROW, str(nova_row))
            if teve_mudanca_banco or forcar_completo:
                _set_setting(conn, CHAVE_ML_LAST_SYNC, agora_str)

            _set_setting(conn, CHAVE_ML_LAST_CHECK, agora_str)
            _set_setting(conn, CHAVE_ML_LAST_STATUS, "ok")
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                _ensure_settings_table(conn)
                _set_setting(conn, CHAVE_ML_LAST_CHECK, agora_str)
                _set_setting(conn, CHAVE_ML_LAST_STATUS, f"erro: {str(exc)[:240]}")
                conn.commit()
            except Exception:
                pass
            raise

        if teve_mudanca_banco:
            try:
                from crm_npd.services.performance import limpar_caches_comerciais

                limpar_caches_comerciais()
            except Exception:
                logger.debug("nao foi possivel limpar cache comercial apos sync ML", exc_info=True)

        return {
            "has_changes": bool(dados.get("has_changes")) or bool(brutas),
            "since_ts": cursor_ts,
            "last_ts": novo_ts,
            "last_row": nova_row,
            "lidas_planilha": len(brutas),
            **res_lote,
        }


def verificar_planilha_ml_na_subida(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Executa a verificacao inicial das notas do Mercado Livre na subida do CRM."""
    if "PYTEST_CURRENT_TEST" in os.environ:
        return None
    if not sync_ml_on_start_ativo():
        logger.info("ml_sheets.subida desligada (ML_SHEETS_SYNC_ON_START=0)")
        return None
    if os.environ.get("FLASK_DEBUG", "0") == "1" and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return None
    if not resolver_webhook_url_ml(conn):
        logger.info("ml_sheets.subida ignorada: URL do webhook vazia")
        return None

    logger.info("ml_sheets.subida verificando notas na planilha Google Sheets...")
    res = sincronizar_planilha_ml_delta(conn)
    logger.info(
        "ml_sheets.subida concluida: novos=%s atualizados=%s removidos=%s last_ts=%s",
        res["novos"],
        res["substituidos"],
        res["removidos"],
        res["last_ts"] or "vazio",
    )
    return res


def ciclo_ml_sheets(*, forcar_completo: bool = False) -> dict[str, Any]:
    """Executa um passo do listener abrindo conexao propria com o SQLite do CRM."""
    from crm_npd.legacy_monolith import conectar, init_db

    init_db()
    with conectar() as sqlite_conn:
        res = sincronizar_planilha_ml_delta(sqlite_conn, forcar_completo=forcar_completo)
    if res["novos"] or res["substituidos"] or res["removidos"]:
        logger.info(
            "ml_sheets.sync novos=%s atualizados=%s removidos=%s last_ts=%s",
            res["novos"],
            res["substituidos"],
            res["removidos"],
            res["last_ts"],
        )
    return res


def loop_ml_sheets(
    *,
    uma_vez: bool = False,
    aguardar_primeiro_ciclo: bool = False,
) -> None:
    """Loop em background que consulta o Timestamp Log da planilha `notas_ml`."""
    intervalo = intervalo_sync_ml()
    logger.info("ml_sheets.inicio intervalo=%ss", intervalo)
    if not uma_vez:
        _ml_parar.wait(intervalo if aguardar_primeiro_ciclo else 1.5)
    while not _ml_parar.is_set():
        try:
            ciclo_ml_sheets()
            if uma_vez:
                return
        except Exception:
            logger.warning(
                "ml_sheets.ciclo falhou; nova tentativa em %ss",
                intervalo,
                exc_info=True,
            )
            if uma_vez:
                raise
        _ml_parar.wait(intervalo)
        if uma_vez:
            break


def iniciar_sync_ml_em_background(*, ja_verificou_na_subida: bool = False) -> bool:
    """Inicia a thread daemon de leitura incremental da planilha `notas_ml`."""
    global _ml_thread
    if "PYTEST_CURRENT_TEST" in os.environ:
        return False
    if not sync_ml_auto_ativo():
        logger.info("ml_sheets.auto desligado (ML_SHEETS_SYNC_AUTO=0)")
        return False
    if os.environ.get("FLASK_DEBUG", "0") == "1" and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return False
    if not resolver_webhook_url_ml():
        logger.info("ml_sheets.auto ignorado: URL do webhook vazia")
        return False

    with _ml_thread_lock:
        if _ml_thread is not None and _ml_thread.is_alive():
            return True
        _ml_parar.clear()
        _ml_thread = threading.Thread(
            target=loop_ml_sheets,
            kwargs={"aguardar_primeiro_ciclo": ja_verificou_na_subida},
            name="ml-sheets-cdc",
            daemon=True,
        )
        _ml_thread.start()
        logger.info(
            "ml_sheets.auto thread iniciada (intervalo=%ss)",
            int(intervalo_sync_ml()),
        )
        return True


def parar_sync_ml_em_background() -> None:
    """Sinaliza parada para a thread de sincronizacao da planilha `notas_ml`."""
    _ml_parar.set()
