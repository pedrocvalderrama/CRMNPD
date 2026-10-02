"""Churn por identidade comercial.

Agrupa pela mesma chave que o resto do CRM usa (``identity_key``), nao pela
coluna ``rede_key`` que esta vazia em todas as notas importadas.
"""

from __future__ import annotations

import sqlite3
from datetime import date


_CHAVE = "COALESCE(NULLIF(TRIM(cir.identity_key), ''), 'CLIENTE:' || i.cliente_id)"
_NOME = (
    "MAX(COALESCE(NULLIF(TRIM(cir.identity_nome), ''), "
    "NULLIF(TRIM(i.rede_nome), ''), NULLIF(TRIM(i.cliente_nome_ref), ''), "
    "'Cliente sem nome'))"
)


def contar_perdidos(
    conn: sqlite3.Connection,
    ano_anterior: int,
    ano_atual: int,
    ate: date,
) -> int:
    """Conta identidades que compraram no ano anterior e nao voltaram.

    Args:
        conn: Conexao SQLite aberta.
        ano_anterior: Ano de referencia da base que perdeu.
        ano_atual: Ano em que se espera a volta.
        ate: Data limite do ano atual (em geral, hoje).

    Returns:
        Quantidade de identidades em churn.
    """
    row = conn.execute(f"""
        WITH base_ly AS (
            SELECT DISTINCT {_CHAVE} AS chave
            FROM invoices i
            LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
            WHERE date(i.data_emissao) BETWEEN date(?) AND date(?)
        ),
        base_cy AS (
            SELECT DISTINCT {_CHAVE} AS chave
            FROM invoices i
            LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
            WHERE date(i.data_emissao) BETWEEN date(?) AND date(?)
        )
        SELECT COUNT(*) AS qtd
        FROM base_ly ly
        LEFT JOIN base_cy cy ON cy.chave = ly.chave
        WHERE cy.chave IS NULL
    """, (
        f"{ano_anterior}-01-01", f"{ano_anterior}-12-31",
        f"{ano_atual}-01-01", ate.isoformat(),
    )).fetchone()
    return int(row["qtd"] or 0)


def top_perdidos(
    conn: sqlite3.Connection,
    ano_anterior: int,
    ano_atual: int,
    ate: date,
    limite: int = 20,
) -> list[sqlite3.Row]:
    """Lista as maiores perdas de faturamento por identidade.

    Args:
        conn: Conexao SQLite aberta.
        ano_anterior: Ano de referencia.
        ano_atual: Ano corrente.
        ate: Data limite do ano corrente.
        limite: Quantidade maxima de linhas.

    Returns:
        Linhas com cliente, valor_ly, valor_cy e perda.
    """
    return list(conn.execute(f"""
        WITH base_ly AS (
            SELECT {_CHAVE} AS chave, {_NOME} AS cliente,
                   COALESCE(SUM(COALESCE(i.valor,0)),0) AS valor_ly
            FROM invoices i
            LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
            WHERE date(i.data_emissao) BETWEEN date(?) AND date(?)
            GROUP BY 1
        ),
        base_cy AS (
            SELECT {_CHAVE} AS chave,
                   COALESCE(SUM(COALESCE(i.valor,0)),0) AS valor_cy
            FROM invoices i
            LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
            WHERE date(i.data_emissao) BETWEEN date(?) AND date(?)
            GROUP BY 1
        )
        SELECT ly.cliente, ly.valor_ly, COALESCE(cy.valor_cy, 0) AS valor_cy,
               (ly.valor_ly - COALESCE(cy.valor_cy, 0)) AS perda
        FROM base_ly ly
        LEFT JOIN base_cy cy ON cy.chave = ly.chave
        WHERE COALESCE(cy.valor_cy, 0) <= 0
        ORDER BY ly.valor_ly DESC, ly.cliente ASC
        LIMIT ?
    """, (
        f"{ano_anterior}-01-01", f"{ano_anterior}-12-31",
        f"{ano_atual}-01-01", ate.isoformat(),
        int(limite),
    )).fetchall())
