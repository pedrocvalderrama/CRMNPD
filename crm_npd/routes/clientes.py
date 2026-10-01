from crm_npd.services.identity import agrupar_clientes
"""Módulo reservado para extração gradual da rota de clientes.

Objetivo:
- isolar a lógica pesada da tela
- permitir correções pontuais em arquivo menor
- manter o layout legado sem regressão

Escopo previsto: Lista consolidada, filtros, score e perfil.
"""
