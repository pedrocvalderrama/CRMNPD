"""Repositorios de persistencia.

A camada de dominio e de metricas nao importa nada daqui. Os servicos e as
rotas pedem dados a um repositorio, nunca montam SQL inline.

Dois backends convivem:

``sqlite``
    Banco do CRM e espelho do ERP. Leitura e escrita.
``firebird``
    ERP Sisplan. Somente SELECT, transacao READ ONLY. Ver
    :mod:`crm_npd.repositories.firebird.conexao`.
"""
