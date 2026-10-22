"""Seletor da fonte primaria de dados do ERP.

A fonte comercial e o Firebird (FDB). O SQLite so guarda o espelho e o que
o CRM cria (tarefas, metas, usuarios, identidades).
"""

from __future__ import annotations

import os

FONTES_VALIDAS = ("sqlite", "firebird")


def fonte_primaria() -> str:
    """Devolve a fonte configurada, default firebird.

    Returns:
        ``sqlite`` ou ``firebird``.
    """
    valor = (os.environ.get("CRM_NPD_FONTE") or "firebird").strip().lower()
    if valor not in FONTES_VALIDAS:
        return "firebird"
    return valor


def usando_firebird() -> bool:
    """Indica se o Sisplan/FDB e a fonte primaria."""
    return fonte_primaria() == "firebird"
