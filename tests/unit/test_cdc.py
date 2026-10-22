"""Parser e classificacao do listener CDC dos logs do Sisplan."""

from __future__ import annotations

from crm_npd.repositories.firebird.conexao import _validar_select
from crm_npd.repositories.firebird.consultas import (
    CRM_FATURAMENTO,
    CRM_ORCAMENTOS,
    CRM_PEDIDOS,
    anexar_and,
    in_trim,
    sql_cdc_log,
    sql_cdc_log_alteracao,
    sql_cdc_max_log,
    sql_cdc_max_log_alteracao,
)
from datetime import date

from crm_npd.sync.cdc import (
    classificar_tabela,
    deve_recarregar_na_subida,
    extrair_chave_documento,
    snapshot_desatualizado,
)
from crm_npd.sync.etl import _rotulo_status_os


def test_chave_pedido_numero_e_ped_num() -> None:
    assert extrair_chave_documento("NUMERO = 22689") == "22689"
    assert extrair_chave_documento("PED_NUM = 22689") == "22689"
    assert extrair_chave_documento("PEDIDO = 22689") == "22689"


def test_chave_fatura_e_os() -> None:
    assert extrair_chave_documento("FATURA = 01135942") == "01135942"
    assert extrair_chave_documento("NOTAFISCAL = 660751") == "660751"
    assert extrair_chave_documento("OS = 10422") == "10422"
    assert extrair_chave_documento("22689") == "22689"


def test_classifica_pedido_nota_e_os() -> None:
    assert classificar_tabela("PEDIDO_001", "") == "pedidos"
    assert classificar_tabela("PED_ITEN_001", "PEDIDOS") == "pedidos"
    assert classificar_tabela("NOTA_001", "") == "faturamento"
    assert classificar_tabela("NOTAITEN_001", "FATURAMENTO") == "faturamento"
    assert classificar_tabela("NOTA_ENTRA_001", "") == "faturamento"
    assert classificar_tabela("RECEBER_001", "") == "faturamento"
    assert classificar_tabela("OS_001", "") == "orcamentos"
    assert classificar_tabela("OS_ITEN_001", "ORCAMENTO") == "orcamentos"


def test_pre_pedido_vai_para_orcamento() -> None:
    assert classificar_tabela("OS_001", "PRE-PEDIDO") == "orcamentos"
    assert classificar_tabela("", "PRE PEDIDO") == "orcamentos"


def test_ignora_tabela_que_o_crm_nao_usa() -> None:
    assert classificar_tabela("ENTIDADE_001", "CADASTRO DE CLIENTE") is None
    assert classificar_tabela("PRODUTO_001", "") is None


def test_sqls_cdc_sao_somente_select() -> None:
    for sql in (
        sql_cdc_max_log_alteracao(),
        sql_cdc_max_log(),
        sql_cdc_log_alteracao(2000),
        sql_cdc_log(500),
        anexar_and(CRM_PEDIDOS, in_trim("P.NUMERO", 2)),
        anexar_and(CRM_ORCAMENTOS, in_trim("O.NUMERO", 1)),
    ):
        assert _validar_select(sql).upper().lstrip().startswith("SELECT")


def test_anexar_and_respeita_where_existente() -> None:
    com_where = anexar_and(CRM_PEDIDOS, "P.NUMERO = ?")
    assert com_where.upper().count("WHERE") == 1
    assert "AND P.NUMERO" in com_where.upper()
    sem_where = anexar_and(CRM_ORCAMENTOS, "O.NUMERO = ?")
    assert "WHERE O.NUMERO" in sem_where.upper()
    fat = anexar_and(CRM_FATURAMENTO, in_trim("N.FATURA", 1))
    assert "AND TRIM(COALESCE(N.FATURA" in fat.upper()
    assert in_trim("P.NUMERO", 3) == "TRIM(COALESCE(P.NUMERO, '')) IN (?,?,?)"


def test_base_de_ontem_pede_carga_completa() -> None:
    hoje = date(2026, 10, 22)
    assert snapshot_desatualizado("2026-10-21 18:40:00", hoje=hoje)
    assert not snapshot_desatualizado("2026-10-22 08:01:00", hoje=hoje)
    assert snapshot_desatualizado("", hoje=hoje)
    assert deve_recarregar_na_subida(
        origem="FIREBIRD",
        synced_at="2026-10-21 18:40:00",
        alvo_salvo="192.168.100.168:3051 -> C:\\Sisplan\\Base\\14215F.FDB",
        alvo_atual="192.168.100.168:3051 -> C:\\Sisplan\\Base\\14215F.FDB",
        modo="stale",
        hoje=hoje,
    )
    assert not deve_recarregar_na_subida(
        origem="FIREBIRD",
        synced_at="2026-10-22 09:00:00",
        alvo_salvo="192.168.100.168:3051 -> C:\\Sisplan\\Base\\14215F.FDB",
        alvo_atual="192.168.100.168:3051 -> C:\\Sisplan\\Base\\14215F.FDB",
        modo="stale",
        hoje=hoje,
    )


def test_troca_de_alvo_ou_csv_pede_carga_completa() -> None:
    hoje = date(2026, 10, 22)
    assert deve_recarregar_na_subida(
        origem="CSV",
        synced_at="2026-10-22 09:00:00",
        alvo_salvo="",
        alvo_atual="192.168.100.168:3051 -> C:\\Sisplan\\Base\\14215F.FDB",
        modo="stale",
        hoje=hoje,
    )
    assert deve_recarregar_na_subida(
        origem="FIREBIRD",
        synced_at="2026-10-22 09:00:00",
        alvo_salvo="arquivo local D:\\dev npd\\CRM NPD\\base\\14215F.FDB",
        alvo_atual="192.168.100.168:3051 -> C:\\Sisplan\\Base\\14215F.FDB",
        modo="stale",
        hoje=hoje,
    )
    assert not deve_recarregar_na_subida(
        origem="FIREBIRD",
        synced_at="2026-10-21 18:40:00",
        alvo_salvo="192.168.100.168:3051 -> C:\\Sisplan\\Base\\14215F.FDB",
        alvo_atual="192.168.100.168:3051 -> C:\\Sisplan\\Base\\14215F.FDB",
        modo="0",
        hoje=hoje,
    )


def test_rotulo_status_os_junta_codigo_e_descricao() -> None:
    assert _rotulo_status_os("40", "MANUTENCAO DE CARTEIRA") == "40 - MANUTENCAO DE CARTEIRA"
    assert _rotulo_status_os("19", "PEDIDO APROVADO") == "19 - PEDIDO APROVADO"
    assert _rotulo_status_os("09", None) == "09 - ORCAMENTO EM NEGOCIACAO"
    assert _rotulo_status_os("", "PEDIDO APROVADO") == "PEDIDO APROVADO"


def test_orcamentos_leem_descricao_do_status_os() -> None:
    sql = CRM_ORCAMENTOS.upper()
    assert "O.STATUS_OS" in sql
    assert "ST.DESCRICAO" in sql
    assert "DESC_STATUS" in sql
    assert "TIPO_MOTIVO_001 ST" in sql
