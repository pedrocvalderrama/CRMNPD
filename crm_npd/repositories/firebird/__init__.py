"""Repositorios Firebird do ERP Sisplan.

REGRA DE OURO: somente SELECT. Toda conexao abre transacao READ ONLY.
Nenhuma funcao deste pacote emite INSERT, UPDATE, DELETE, DDL ou procedure.
"""

from crm_npd.repositories.firebird.conexao import (
    caminho_fdb,
    conectar_firebird,
    conectar_somente_leitura,
    descricao_conexao,
    diagnosticar_saude_firebird,
    executar_select,
    executar_select_dicts,
    tpb_monitoramento,
)

__all__ = [
    "caminho_fdb",
    "conectar_firebird",
    "conectar_somente_leitura",
    "descricao_conexao",
    "diagnosticar_saude_firebird",
    "executar_select",
    "executar_select_dicts",
    "tpb_monitoramento",
]
