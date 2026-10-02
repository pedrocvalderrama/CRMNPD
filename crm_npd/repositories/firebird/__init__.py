"""Repositorios Firebird do ERP Sisplan.

REGRA DE OURO: somente SELECT. Toda conexao abre transacao READ ONLY.
Nenhuma funcao deste pacote emite INSERT, UPDATE, DELETE, DDL ou procedure.
"""

from crm_npd.repositories.firebird.conexao import conectar_somente_leitura, executar_select

__all__ = ["conectar_somente_leitura", "executar_select"]
