"""Consultas de orcamentos no SQLite do CRM."""

from __future__ import annotations

import sqlite3
from datetime import date

from crm_npd.metrics.funil import orcamento_convertido


def no_periodo(
    conn: sqlite3.Connection,
    inicio: date | str,
    fim: date | str,
    vendor_id: str | None = None,
    vendor_nome: str | None = None,
) -> list[sqlite3.Row]:
    """Lista orcamentos emitidos no periodo.

    Args:
        conn: Conexao SQLite aberta.
        inicio: Primeiro dia.
        fim: Ultimo dia.
        vendor_id: Codigo do vendedor, se houver.
        vendor_nome: Nome do vendedor, se houver.

    Returns:
        Linhas de orcamento.
    """
    sql = """
        SELECT numero, pedido_id, valor_total, status, desc_status,
               vendedor_id, vendedor_nome
        FROM orcamentos
        WHERE date(data_inicio) BETWEEN date(?) AND date(?)
    """
    params: list[object] = [inicio.isoformat() if isinstance(inicio, date) else str(inicio),
                            fim.isoformat() if isinstance(fim, date) else str(fim)]
    filtros: list[str] = []
    if vendor_id:
        v_str = str(vendor_id).strip()
        v_norm = v_str.lstrip("0")
        if v_norm:
            filtros.append("(TRIM(COALESCE(vendedor_id,'')) = ? OR TRIM(COALESCE(vendedor_id,'')) = ?)")
            params.extend([v_str, v_norm])
        else:
            filtros.append("(TRIM(COALESCE(vendedor_id,'')) = ?)")
            params.append(v_str)
    if vendor_nome:
        filtros.append("UPPER(TRIM(COALESCE(vendedor_nome,''))) = UPPER(?)")
        params.append(str(vendor_nome).strip())
    if filtros:
        sql += " AND (" + " OR ".join(filtros) + ")"
    return list(conn.execute(sql, params).fetchall())


def metricas(
    conn: sqlite3.Connection,
    inicio: date | str,
    fim: date | str,
    vendor_id: str | None = None,
    vendor_nome: str | None = None,
) -> dict[str, float | int]:
    """Consolida totais e conversao do periodo com a regra unica aprovada.

    Args:
        conn: Conexao SQLite aberta.
        inicio: Primeiro dia.
        fim: Ultimo dia.
        vendor_id: Codigo do vendedor, se houver.
        vendor_nome: Nome do vendedor, se houver.

    Returns:
        Dicionario com total, convertidos, valor_total e taxa_conversao.
    """
    rows = no_periodo(conn, inicio, fim, vendor_id=vendor_id, vendor_nome=vendor_nome)
    total = len(rows)
    convertidos = sum(1 for r in rows if orcamento_convertido(r["pedido_id"], r["desc_status"]))
    valor = sum(float(r["valor_total"] or 0) for r in rows)
    return {
        "total": total,
        "convertidos": convertidos,
        "valor_total": valor,
        "taxa_conversao": (convertidos / total * 100.0) if total else 0.0,
    }
