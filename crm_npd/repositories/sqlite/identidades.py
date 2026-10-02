"""Consultas de identidade comercial no SQLite do CRM."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable


CHAVE_IDENTIDADE = "COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || {alias}.cliente_id)"


def mapa_cliente_identidade(conn: sqlite3.Connection) -> dict[int, str]:
    """Devolve o mapa cliente_id -> identity_key.

    Args:
        conn: Conexao SQLite aberta.

    Returns:
        Dicionario com uma entrada por cliente cadastrado.
    """
    rows = conn.execute("""
        SELECT c.id,
               COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || c.id) AS identity_key
        FROM customers c
        LEFT JOIN customer_identity_rules cir ON cir.customer_id = c.id
    """).fetchall()
    return {int(r["id"]): (r["identity_key"] or f"CLIENTE:{r['id']}") for r in rows}


def primeiro_faturamento(conn: sqlite3.Connection, identity_key: str) -> sqlite3.Row | None:
    """Primeiro faturamento historico de uma identidade.

    Args:
        conn: Conexao SQLite aberta.
        identity_key: Chave comercial (GRUPO:/REDE:/CLIENTE:).

    Returns:
        Linha com id, numero, pedido e data_emissao, ou None.
    """
    return conn.execute("""
        SELECT i.id, i.numero, i.pedido, i.data_emissao
        FROM invoices i
        LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
        WHERE COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || i.cliente_id) = ?
          AND i.data_emissao IS NOT NULL
        ORDER BY date(i.data_emissao) ASC, i.id ASC
        LIMIT 1
    """, (identity_key,)).fetchone()


def faturamentos_com_data(
    conn: sqlite3.Connection,
    customer_ids: Iterable[int] | None = None,
) -> list[sqlite3.Row]:
    """Lista faturamentos com valor e data, opcionalmente filtrados por cliente.

    Args:
        conn: Conexao SQLite aberta.
        customer_ids: Subconjunto de clientes. None le toda a base.

    Returns:
        Linhas ordenadas por data.
    """
    sql = """
        SELECT i.cliente_id, i.data_emissao, COALESCE(i.valor, 0) AS valor,
               COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || i.cliente_id) AS identity_key
        FROM invoices i
        LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
        WHERE COALESCE(i.valor, 0) > 0
          AND COALESCE(i.data_emissao, '') <> ''
    """
    params: list[object] = []
    ids = [int(x) for x in (customer_ids or []) if x]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        sql += f" AND i.cliente_id IN ({placeholders})"
        params.extend(ids)
    sql += " ORDER BY i.data_emissao ASC"
    return list(conn.execute(sql, params).fetchall())
