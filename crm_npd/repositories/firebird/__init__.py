"""Repositorios Firebird do ERP Sisplan.

REGRA DE OURO: somente SELECT. Toda conexao abre transacao READ ONLY.
Nenhuma funcao deste pacote emite INSERT, UPDATE, DELETE, DDL ou procedure.
"""

from crm_npd.repositories.firebird.conexao import (
    caminho_fdb,
    conectar_somente_leitura,
    descricao_conexao,
    executar_select,
    executar_select_dicts,
)

__all__ = [
    "caminho_fdb",
    "conectar_somente_leitura",
    "descricao_conexao",
    "executar_select",
    "executar_select_dicts",
]
