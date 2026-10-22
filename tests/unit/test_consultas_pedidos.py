"""Regras do MANUAL_REGRAS_PEDIDOS_SISPLAN na extração Firebird."""

from crm_npd.legacy_monolith import sql_pedidos_realizados_excel_cond
from crm_npd.repositories.firebird.consultas import CRM_PEDIDOS
from crm_npd.sync.etl import (
    carteira_do_responsavel,
    eh_responsavel_anfarma,
    vendedor_faturamento,
    vendedor_pedido,
)


def test_pedidos_somam_qtde_faturada_e_cancelada() -> None:
    assert "QTDE_CANC" in CRM_PEDIDOS
    assert "QTDE_F" in CRM_PEDIDOS


def test_pedidos_excluem_somente_98_jit() -> None:
    sql = CRM_PEDIDOS.upper()
    assert "98 - JIT" in sql
    assert "05 - JIT IMEDIATO" not in sql
    assert "CLICHES" in sql


def test_pedidos_nao_filtram_carteira_de_responsaveis() -> None:
    sql = CRM_PEDIDOS.upper()
    assert "MARIA CECILIA" not in sql
    assert "ANFARMA" not in sql
    assert "ALLOWED_RESPONSAVEIS" not in sql


def test_pedidos_trazem_codigo_do_responsavel() -> None:
    sql = CRM_PEDIDOS.upper()
    assert "P.RESPONSAVEL" in sql
    assert "RESPONSAVEL_COD" in sql


def test_pedido_agrupa_por_responsavel_nao_representante() -> None:
    assert vendedor_pedido({
        "RESPONSAVEL": "13309 - MARIA CECILIA VOLPATO DE FREITAS",
        "REP_NOME": "SITE",
    }) == "13309 - MARIA CECILIA VOLPATO DE FREITAS"
    assert vendedor_pedido({"RESPONSAVEL_NOME": "ROSE DE MORAIS"}) == "ROSE DE MORAIS"


def test_faturamento_usa_responsavel_do_pedido_nao_rep_da_nota() -> None:
    assert vendedor_faturamento({
        "RESPONSAVEL": "12667 - JOSEANE VOLINGER DOS SANTOS",
        "REP_NOME": "09044 - AMANDA LONGARAI KOSSMAN",
    }) == "12667 - JOSEANE VOLINGER DOS SANTOS"
    assert vendedor_faturamento({"REP_NOME": "09044 - AMANDA LONGARAI KOSSMAN"}) == "09044 - AMANDA LONGARAI KOSSMAN"


def test_anfarma_entra_na_carteira_da_joseane() -> None:
    origem = "05975 - ASSOCIACAO NACIONAL DE FARMACEUTICOS MAGISTRAIS ANFARMA"
    assert eh_responsavel_anfarma(origem) is True
    assert eh_responsavel_anfarma("12667 - JOSEANE VOLINGER DOS SANTOS") is False
    assert eh_responsavel_anfarma("05240 - ANFARMAG INATIVO") is False
    assert carteira_do_responsavel(origem) == "12667 - JOSEANE VOLINGER DOS SANTOS"
    assert vendedor_pedido({"RESPONSAVEL": origem}) == origem


def test_realizado_inclui_05_jit_imediato() -> None:
    sql = sql_pedidos_realizados_excel_cond("o", "v").upper()
    assert "05%JIT%IMEDIATO" not in sql
    assert "GENERAL LYY" not in sql
    assert "98%JIT" in sql
    assert "CLICHES" in sql
