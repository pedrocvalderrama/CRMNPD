"""Testes de funil, periodos, percentuais e score do vendedor."""

from __future__ import annotations

from datetime import date

from crm_npd.domain.enums import EtapaFunil
from crm_npd.metrics import funil as f
from crm_npd.metrics import percentuais as p
from crm_npd.metrics import periodos as per
from crm_npd.metrics import score_vendedor as sv


def test_pedido_vinculado_ganha_de_status() -> None:
    assert f.determinar_etapa("CANCELADO", pedido_id="123") is EtapaFunil.GANHO


def test_textos_de_desfecho() -> None:
    assert f.determinar_etapa("PEDIDO APROVADO") is EtapaFunil.GANHO
    assert f.determinar_etapa("CANCELADO") is EtapaFunil.PERDIDO
    assert f.determinar_etapa("NEGOCIACAO DE PRECO") is EtapaFunil.NEGOCIACAO
    assert f.determinar_etapa("CONTATO FUTURO") is EtapaFunil.CONTATO
    assert f.determinar_etapa("", status_code="F") is EtapaFunil.ENCERRADO
    assert f.determinar_etapa(None) is EtapaFunil.NOVO


def test_orcamento_convertido_regra_unica() -> None:
    assert f.orcamento_convertido("123") is True
    assert f.orcamento_convertido("0") is True
    assert f.orcamento_convertido("None") is True
    assert f.orcamento_convertido("", "PEDIDO APROVADO") is True
    assert f.orcamento_convertido("", "EM NEGOCIACAO") is False
    assert f.orcamento_convertido(None, None) is False


def test_taxa_conversao_e_faturamento_divisao_por_zero() -> None:
    assert f.taxa_conversao(5, 10) == 50.0
    assert f.taxa_conversao(1, 0) == 0.0
    assert f.taxa_faturamento_pedidos(8, 10) == 80.0
    assert f.taxa_faturamento_pedidos(8, 0) == 0.0


def test_sla_parado() -> None:
    assert f.esta_parado(EtapaFunil.NEGOCIACAO, 14) is True
    assert f.esta_parado(EtapaFunil.GANHO, 90) is False
    assert f.esta_parado(EtapaFunil.NOVO, 30) is False


def test_ciclo_faturamento_doctest() -> None:
    periodo = per.ciclo_faturamento(date(2026, 4, 10), 24)
    assert periodo.inicio == date(2026, 3, 25)
    assert periodo.fim == date(2026, 4, 24)


def test_ciclo_faturamento_padrao_26_para_25() -> None:
    periodo = per.ciclo_faturamento(date(2026, 4, 10))
    assert periodo.inicio == date(2026, 3, 26)
    assert periodo.fim == date(2026, 4, 25)
    assert per.DIA_FECHAMENTO_PADRAO == 25


def test_ciclo_fevereiro_e_virada_de_ano() -> None:
    fev = per.ciclo_faturamento(date(2026, 2, 10), 31)
    assert fev.fim.month == 2
    jan = per.ciclo_faturamento(date(2026, 1, 10), 24)
    assert jan.inicio == date(2025, 12, 25)


def test_percentuais_divisao_por_zero() -> None:
    assert p.percentual(10, 0) == 0.0
    assert p.percentual(None, 10) == 0.0  # type: ignore[arg-type]
    assert p.variacao_percentual(50, 0) == 100.0
    assert p.variacao_percentual(0, 0) == 0.0
    assert p.variacao_percentual_ou_nulo(50, 0) is None
    assert p.projecao_linear(100, 10, 20) == 200.0
    assert p.projecao_linear(100, 0, 20) == 0.0


def test_score_vendedor_sem_atividade_e_zero() -> None:
    dims = sv.calcular_dimensoes(
        orcamentos_qtd=0,
        orcamentos_valor=0.0,
        faturamento_valor=0.0,
        conversao_pct=0.0,
        carteira_pontos=0.0,
        variacao_orcamentos_pct=None,
        variacao_conversao_pp=0.0,
        max_orcamentos_qtd=10,
        max_orcamentos_valor=1_000.0,
        max_faturamento=1_000.0,
        max_carteira_pontos=1.0,
    )
    assert 0.0 <= sv.calcular_score(dims) < 5.0


def test_pontos_carteira() -> None:
    assert sv.pontos_carteira(1, 0, 0) == sv.PONTOS_CLIENTE_NOVO
    assert sv.pontos_carteira(0, 1, 0) == sv.PONTOS_CLIENTE_RECORRENTE
    assert sv.pontos_carteira(0, 1, 1) == sv.PONTOS_CLIENTE_RECUPERADO
