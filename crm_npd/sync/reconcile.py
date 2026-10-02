"""Compara o espelho Firebird com os totais de controle do CRM.

O valor de controle de setembro/2026 e R$ 997.044,89 em 518 linhas de
faturamento. O cutover da Fase 5 so acontece quando a diferenca for zero.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from crm_npd.repositories.sqlite.faturamento import total_periodo

CONTROLE_SET_2026 = {
    "inicio": date(2026, 8, 25),
    "fim": date(2026, 9, 24),
    "valor": 997_044.89,
    "linhas": 518,
}


def reconciliar_faturamento(
    conn: sqlite3.Connection,
    inicio: date,
    fim: date,
    valor_esperado: float,
    linhas_esperadas: int,
) -> dict[str, Any]:
    """Compara o faturamento do SQLite com um total de controle.

    Args:
        conn: Conexao SQLite (somente leitura basta).
        inicio: Inicio do ciclo.
        fim: Fim do ciclo.
        valor_esperado: Total oficial, em reais.
        linhas_esperadas: Quantidade oficial de linhas.

    Returns:
        Relatorio com diferencas. ``ok`` e True somente quando as duas
        diferencas sao zero.
    """
    valor, linhas = total_periodo(conn, inicio, fim)
    delta_valor = round(valor - float(valor_esperado), 2)
    delta_linhas = int(linhas) - int(linhas_esperadas)
    return {
        "inicio": inicio.isoformat(),
        "fim": fim.isoformat(),
        "valor_lido": valor,
        "valor_esperado": float(valor_esperado),
        "delta_valor": delta_valor,
        "linhas_lidas": linhas,
        "linhas_esperadas": int(linhas_esperadas),
        "delta_linhas": delta_linhas,
        "ok": delta_valor == 0.0 and delta_linhas == 0,
    }


def reconciliar_controle_setembro(conn: sqlite3.Connection) -> dict[str, Any]:
    """Roda a reconciliacao contra o total oficial de set/2026."""
    c = CONTROLE_SET_2026
    return reconciliar_faturamento(conn, c["inicio"], c["fim"], c["valor"], c["linhas"])
