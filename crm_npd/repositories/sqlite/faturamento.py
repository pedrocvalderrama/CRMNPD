"""Consultas de faturamento e pedidos no SQLite do CRM."""

from __future__ import annotations

import sqlite3
from datetime import date


def total_periodo(conn: sqlite3.Connection, inicio: date, fim: date) -> tuple[float, int]:
    """Soma o faturamento de um intervalo de datas.

    Args:
        conn: Conexao SQLite aberta.
        inicio: Primeiro dia, inclusive.
        fim: Ultimo dia, inclusive.

    Returns:
        Tupla (valor, quantidade de linhas).
    """
    row = conn.execute("""
        SELECT COALESCE(SUM(COALESCE(valor, 0)), 0) AS valor,
               COUNT(*) AS qtd
        FROM invoices
        WHERE date(data_emissao) BETWEEN date(?) AND date(?)
    """, (inicio.isoformat(), fim.isoformat())).fetchone()
    return float(row["valor"] or 0), int(row["qtd"] or 0)


def linhas_ciclo(
    conn: sqlite3.Connection,
    inicio: date,
    fim: date,
    vendor_id: int | None = None,
) -> list[sqlite3.Row]:
    """Linhas de nota de um ciclo, com identidade comercial.

    Args:
        conn: Conexao SQLite aberta.
        inicio: Inicio do ciclo.
        fim: Fim do ciclo.
        vendor_id: Filtra por vendedor quando informado.

    Returns:
        Linhas de faturamento.
    """
    sql = """
        SELECT i.id, i.numero, i.pedido, i.data_emissao, COALESCE(i.valor, 0) AS valor,
               i.cliente_id, i.vendor_id, COALESCE(i.is_jit, 0) AS is_jit,
               COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || i.cliente_id) AS identity_key
        FROM invoices i
        LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
        WHERE date(i.data_emissao) BETWEEN date(?) AND date(?)
    """
    params: list[object] = [inicio.isoformat(), fim.isoformat()]
    if vendor_id:
        sql += " AND i.vendor_id = ?"
        params.append(int(vendor_id))
    sql += " ORDER BY date(i.data_emissao) ASC, i.id ASC"
    return list(conn.execute(sql, params).fetchall())
