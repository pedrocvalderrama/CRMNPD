"""Testes dos repositorios SQLite e do reconcile, sobre a fixture congelada."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from crm_npd.repositories.sqlite import churn, faturamento, orcamentos
from crm_npd.sync.reconcile import reconciliar_faturamento

FIXTURE = Path(__file__).resolve().parents[1] / "golden" / "fixture" / "baseline.db"


@pytest.fixture(scope="module")
def conn() -> sqlite3.Connection:
    if not FIXTURE.exists():
        pytest.skip("fixture golden ausente")
    c = sqlite3.connect(f"file:{FIXTURE.as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_faturamento_ciclo_setembro(conn: sqlite3.Connection) -> None:
    valor, linhas = faturamento.total_periodo(conn, date(2026, 8, 25), date(2026, 9, 24))
    assert linhas > 0
    assert valor > 0


def test_orcamentos_conversao_alinhada(conn: sqlite3.Connection) -> None:
    m = orcamentos.metricas(conn, date(2026, 1, 1), date(2026, 10, 1))
    assert m["total"] == 1541
    assert m["convertidos"] == 895
    assert abs(float(m["taxa_conversao"]) - 58.08) < 0.05


def test_churn_por_identidade(conn: sqlite3.Connection) -> None:
    qtd = churn.contar_perdidos(conn, 2025, 2026, date(2026, 10, 1))
    assert qtd == 778


def test_reconcile_reporta_delta(conn: sqlite3.Connection) -> None:
    rel = reconciliar_faturamento(conn, date(2026, 8, 25), date(2026, 9, 24), 997_044.89, 518)
    assert "delta_valor" in rel
    assert "ok" in rel
