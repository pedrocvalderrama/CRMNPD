"""Consultas de vendedores no SQLite do CRM."""

from __future__ import annotations

import sqlite3


def catalogo(conn: sqlite3.Connection, *, incluir_inativos: bool = False) -> list[sqlite3.Row]:
    """Lista o cadastro de vendedores.

    Args:
        conn: Conexao SQLite aberta.
        incluir_inativos: Se True, inclui vendedores com ativo = 0.

    Returns:
        Linhas de vendedores.
    """
    sql = "SELECT id, codigo, nome, tipo, ativo FROM vendors"
    if not incluir_inativos:
        sql += " WHERE COALESCE(ativo, 1) = 1"
    sql += " ORDER BY nome"
    return list(conn.execute(sql).fetchall())


def por_id(conn: sqlite3.Connection, vendor_id: int) -> sqlite3.Row | None:
    """Busca um vendedor pelo id interno.

    Args:
        conn: Conexao SQLite aberta.
        vendor_id: Identificador.

    Returns:
        Linha do vendedor, ou None.
    """
    return conn.execute(
        "SELECT id, codigo, nome, tipo, ativo FROM vendors WHERE id = ?",
        (int(vendor_id),),
    ).fetchone()
