"""Seletor da fonte primaria de dados do ERP.

O cutover so acontece quando ``reconciliar_controle_setembro`` devolve ok.
Ate la, ``CRM_NPD_FONTE`` permanece ``sqlite`` e o CSV continua no lugar.
"""

from __future__ import annotations

import os

FONTES_VALIDAS = ("sqlite", "firebird")


def fonte_primaria() -> str:
    """Devolve a fonte configurada, default sqlite.

    Returns:
        ``sqlite`` ou ``firebird``.
    """
    valor = (os.environ.get("CRM_NPD_FONTE") or "sqlite").strip().lower()
    if valor not in FONTES_VALIDAS:
        return "sqlite"
    return valor


def usando_firebird() -> bool:
    """Indica se o sync do ERP ja e a fonte primaria."""
    return fonte_primaria() == "firebird"
