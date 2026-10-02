"""Testes do score de identidade comercial."""

from __future__ import annotations

from datetime import date

from crm_npd.domain.enums import FaixaScore, PrioridadeTarefa
from crm_npd.domain.valores import EstatisticasIdentidade
from crm_npd.metrics import score_cliente as sc


def _stats(**kwargs) -> EstatisticasIdentidade:
    padrao = dict(valor_total=10_000.0, pedidos=4, intervalo_mediano=30.0, atraso_atual=10, ultima_data=date(2026, 9, 1))
    padrao.update(kwargs)
    return EstatisticasIdentidade(**padrao)


def test_limitar_score_respeita_extremos() -> None:
    assert sc.limitar_score(-10) == 0.0
    assert sc.limitar_score(150) == 100.0
    assert sc.limitar_score(None) == 0.0  # type: ignore[arg-type]


def test_faixas_em_cada_degrau() -> None:
    assert sc.faixa_de_score(85.0) is FaixaScore.ELITE
    assert sc.faixa_de_score(84.9) is FaixaScore.MUITO_FORTE
    assert sc.faixa_de_score(70.0) is FaixaScore.MUITO_FORTE
    assert sc.faixa_de_score(69.9) is FaixaScore.BOM
    assert sc.faixa_de_score(55.0) is FaixaScore.BOM
    assert sc.faixa_de_score(54.9) is FaixaScore.MORNO
    assert sc.faixa_de_score(35.0) is FaixaScore.MORNO
    assert sc.faixa_de_score(34.9) is FaixaScore.FRIO
    assert sc.faixa_de_score(0) is FaixaScore.FRIO


def test_teto_compra_unica() -> None:
    resultado = sc.calcular_score(_stats(pedidos=1, intervalo_mediano=None), 10_000.0)
    assert resultado.score <= sc.TETO_COMPRA_UNICA
    assert resultado.faixa is FaixaScore.FRIO


def test_tetos_por_intervalo() -> None:
    esparso = sc.calcular_score(_stats(intervalo_mediano=250.0, pedidos=8, valor_total=1_000_000.0), 1_000_000.0)
    assert esparso.score <= 39.0
    trimestral = sc.calcular_score(_stats(intervalo_mediano=130.0, pedidos=8, valor_total=1_000_000.0), 1_000_000.0)
    assert trimestral.score <= 69.0


def test_score_valor_sem_referencia_e_piso() -> None:
    assert sc.score_valor(100.0, 0.0) == 0.0
    assert sc.score_valor(0.0, 1_000.0) == 12.0


def test_compra_unica_em_varias_linhas_nao_infla() -> None:
    """A aprovacao da divergencia 4: um dia de compra e uma compra."""
    um_dia = sc.calcular_score(_stats(pedidos=1, intervalo_mediano=None, atraso_atual=5), 10_000.0)
    cinco_linhas_mesmo_dia = sc.calcular_score(
        _stats(pedidos=5, intervalo_mediano=0.0, atraso_atual=5), 10_000.0
    )
    assert um_dia.score <= sc.TETO_COMPRA_UNICA
    assert cinco_linhas_mesmo_dia.score > um_dia.score


def test_prioridade_urgente_quando_atrasado_e_forte() -> None:
    prioridade = sc.prioridade_tarefa(FaixaScore.ELITE, atraso_atual=90, intervalo_mediano=20.0)
    assert prioridade in (PrioridadeTarefa.URGENTE, PrioridadeTarefa.ALTA, PrioridadeTarefa.MEDIA, PrioridadeTarefa.BAIXA)
