"""Regras do MANUAL_REGRAS_FATURAMENTO_SISPLAN na extração Firebird."""

from crm_npd.repositories.firebird.consultas import (
    CRM_FATURAMENTO,
    CRM_FATURAMENTO_DEMO,
    CRM_FATURAMENTO_ENTRADA,
    FATURAMENTO_LIQUIDO,
)


def test_saida_exclui_cliches_e_nao_soma_frete() -> None:
    sql = CRM_FATURAMENTO.upper()
    assert "CLICHES" in sql
    assert "VALOR_LIQ" in sql
    assert "VALOR_FRETE" not in sql
    assert "5.116" not in sql
    assert "6.1162" not in sql
    assert "MOTIVO_CANCEL" in sql


def test_saida_traz_responsavel_do_pedido() -> None:
    sql = CRM_FATURAMENTO.upper()
    assert "PEDIDO_001" in sql
    assert "RESPONSAVEL" in sql
    assert "NI.PEDIDO" in sql


def test_entrada_e_demo_tambem_trazem_responsavel_do_pedido() -> None:
    assert "PEDIDO_001" in CRM_FATURAMENTO_ENTRADA.upper()
    assert "RESPONSAVEL" in CRM_FATURAMENTO_ENTRADA.upper()
    assert "PEDIDO_001" in CRM_FATURAMENTO_DEMO.upper()
    assert "RESPONSAVEL" in CRM_FATURAMENTO_DEMO.upper()


def test_entrada_devolucao_deduz_sem_frete() -> None:
    sql = CRM_FATURAMENTO_ENTRADA.upper()
    assert "NOTA_ENTRA_001" in sql
    assert "1.201" in sql
    assert "2.201" in sql
    assert "CLICHES" in sql
    assert "-ABS" in sql
    assert "VAL_FRETE" not in sql
    assert "CODSIT_NOTA" in sql


def test_demo_usa_duplicata_emp_id_3() -> None:
    sql = CRM_FATURAMENTO_DEMO.upper()
    assert "EMP_ID = 3" in sql
    assert "STATUS = 'DUPL'" in sql
    assert "VALOR2" in sql
    assert "VAL_DEV" in sql


def test_liquido_periodico_segue_as_mesmas_exclusoes() -> None:
    sql = FATURAMENTO_LIQUIDO.upper()
    assert "CLICHES" in sql
    assert "VALOR_FRETE" not in sql
    assert "5.116" not in sql
