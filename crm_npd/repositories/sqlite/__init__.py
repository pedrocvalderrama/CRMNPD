"""Repositorios SQLite do CRM.

Cada modulo encapsula as consultas de uma area. A conexao e recebida pronta
(WAL, foreign_keys) de :func:`crm_npd.db.connect`.
"""

from crm_npd.repositories.sqlite import churn, faturamento, identidades, orcamentos, vendedores

__all__ = ["churn", "faturamento", "identidades", "orcamentos", "vendedores"]
