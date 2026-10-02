"""Copia SELECT-only do Sisplan para o espelho SQLite.

Nao apaga o historico CSV. Cada execucao registra origem='FIREBIRD' nas linhas
novas e deixa a importacao manual intacta, para fallback de um ciclo.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from typing import Any

from crm_npd.repositories.firebird.conexao import disponivel, executar_select
from crm_npd.repositories.firebird.consultas import FATURAMENTO_LIQUIDO, LIQUIDEZ, PEDIDOS

logger = logging.getLogger(__name__)


def sincronizar_periodo(conn: sqlite3.Connection, inicio: date, fim: date) -> dict[str, int]:
    """Le o ERP no periodo e grava o espelho local.

    Args:
        conn: Conexao SQLite do CRM (escrita permitida aqui, nao no Firebird).
        inicio: Primeiro dia.
        fim: Ultimo dia.

    Returns:
        Contagem de linhas lidas por origem.

    Raises:
        RuntimeError: Se o Firebird nao estiver configurado.
    """
    if not disponivel():
        raise RuntimeError(
            "Firebird indisponivel. Configure SISPLAN_* no .env e instale fbclient."
        )

    lidas = {
        "faturamento": _espelhar(conn, "erp_faturamento", FATURAMENTO_LIQUIDO, inicio, fim),
        "pedidos": _espelhar(conn, "erp_pedidos", PEDIDOS, inicio, fim),
        "liquidez": _espelhar(conn, "erp_liquidez", LIQUIDEZ, inicio, fim),
    }
    conn.commit()
    logger.info("sync.periodo %s %s %s", inicio, fim, lidas)
    return lidas


def _espelhar(
    conn: sqlite3.Connection,
    tabela: str,
    sql: str,
    inicio: date,
    fim: date,
) -> int:
    """Garante a tabela-espelho e grava as linhas do periodo.

    Args:
        conn: Conexao SQLite.
        tabela: Nome da tabela-espelho.
        sql: SELECT no Firebird.
        inicio: Primeiro dia.
        fim: Ultimo dia.

    Returns:
        Quantidade de linhas gravadas.
    """
    _garantir_espelho(conn, tabela)
    conn.execute(
        f"DELETE FROM {tabela} WHERE date(data_ref) BETWEEN date(?) AND date(?)",
        (inicio.isoformat(), fim.isoformat()),
    )
    rows = executar_select(sql, (inicio, fim))
    for row in rows:
        conn.execute(
            f"INSERT INTO {tabela} (data_ref, payload_json, origem, imported_at) VALUES (?, ?, 'FIREBIRD', datetime('now'))",
            (str(row[0])[:10] if row else inicio.isoformat(), _serializar(row)),
        )
    return len(rows)


def _garantir_espelho(conn: sqlite3.Connection, tabela: str) -> None:
    """Cria a tabela-espelho se ainda nao existir."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {tabela} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_ref TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            origem TEXT NOT NULL DEFAULT 'FIREBIRD',
            imported_at TEXT NOT NULL
        )
    """)
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{tabela}_data ON {tabela}(data_ref)")


def _serializar(row: Any) -> str:
    """Serializa uma linha do Firebird em JSON textual."""
    import json

    if hasattr(row, "__iter__") and not isinstance(row, (str, bytes)):
        return json.dumps([_jsonivel(c) for c in row], ensure_ascii=False)
    return json.dumps(_jsonivel(row), ensure_ascii=False)


def _jsonivel(valor: Any) -> Any:
    """Converte tipos Firebird para tipos JSON."""
    if valor is None or isinstance(valor, (str, int, float, bool)):
        return valor
    return str(valor)
