"""Metas de equipe separadas em pedidos e faturamento."""

from __future__ import annotations

import sqlite3

from crm_npd.legacy_monolith import (
    ensure_goals_table,
    get_goal_history,
    get_meta_equipe,
    set_goal_history,
    set_meta_equipe,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_goals_table(conn)
    return conn


def test_grava_pedidos_e_faturamento_separados() -> None:
    conn = _conn()
    set_meta_equipe(conn, "2026-10", "pedidos", 1000.0, 2000.0)
    set_meta_equipe(conn, "2026-10", "faturamento", 3000.0, 4000.0)
    assert get_meta_equipe(conn, "2026-10", "pedidos") == (1000.0, 2000.0)
    assert get_meta_equipe(conn, "2026-10", "faturamento") == (3000.0, 4000.0)
    assert get_goal_history(conn, "2026-10", "PEDIDOS_INTERNA") == 1000.0
    assert get_goal_history(conn, "2026-10", "FATURAMENTO_EXTERNO") == 4000.0


def test_pedidos_cai_no_legado_equipe_quando_ainda_nao_migrado() -> None:
    conn = _conn()
    set_goal_history(conn, "2026-09", "EQUIPE_INTERNA", 150.0)
    set_goal_history(conn, "2026-09", "EQUIPE_EXTERNO", 250.0)
    assert get_meta_equipe(conn, "2026-09", "pedidos") == (150.0, 250.0)
    assert get_meta_equipe(conn, "2026-09", "faturamento") == (0.0, 0.0)


def test_pedidos_novo_tem_prioridade_sobre_legado() -> None:
    conn = _conn()
    set_goal_history(conn, "2026-08", "EQUIPE_INTERNA", 10.0)
    set_goal_history(conn, "2026-08", "EQUIPE_EXTERNO", 20.0)
    set_meta_equipe(conn, "2026-08", "pedidos", 111.0, 222.0)
    assert get_meta_equipe(conn, "2026-08", "pedidos") == (111.0, 222.0)
