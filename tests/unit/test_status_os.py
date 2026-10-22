"""Rotulo de STATUS_OS: codigo do Sisplan ao lado da descricao do cadastro."""

from crm_npd.domain.status_os import codigo_status_os, rotulo_status_os


def test_rotulo_usa_cadastro_quando_so_vem_o_codigo() -> None:
    assert rotulo_status_os("40") == "40 - MANUTENCAO DE CARTEIRA"
    assert rotulo_status_os("09") == "09 - ORCAMENTO EM NEGOCIACAO"
    assert rotulo_status_os("43") == "43 - MANUTENCAO DE INATIVO"
    assert rotulo_status_os("21") == "21 - CANCELADO PELO CLIENTE"
    assert rotulo_status_os("29") == "29 - CLIENTE NAO RETORNOU"
    assert rotulo_status_os("33") == "33 - ORCAMENTO PERDIDO PRECO"


def test_rotulo_nao_duplica_quando_ja_tem_descricao() -> None:
    assert rotulo_status_os("40", "MANUTENCAO DE CARTEIRA") == "40 - MANUTENCAO DE CARTEIRA"
    assert rotulo_status_os("40 - MANUTENCAO DE CARTEIRA") == "40 - MANUTENCAO DE CARTEIRA"
    assert rotulo_status_os("PEDIDO APROVADO") == "PEDIDO APROVADO"


def test_codigo_status_os_aceita_rotulo_ou_numero() -> None:
    assert codigo_status_os("40") == "40"
    assert codigo_status_os("09") == "09"
    assert codigo_status_os("9") == "09"
    assert codigo_status_os("40 - MANUTENCAO DE CARTEIRA") == "40"
    assert codigo_status_os("PEDIDO APROVADO") == ""
