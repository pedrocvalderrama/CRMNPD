"""Congelamento de ambiente para captura golden-master reprodutivel.

O monolito chama ``date.today()`` dentro de dezenas de calculos (status de
cliente, projecao de ciclo, metas). Sem congelar a data, a mesma base produz
numeros diferentes a cada dia e a comparacao golden-master perde sentido.

Como o monolito faz ``from datetime import date``, a classe ``date`` virou um
atributo do modulo. Substituir esse atributo por uma subclasse com ``today()``
fixo e suficiente para tornar toda a leitura deterministica, sem alterar o
codigo legado.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import date
from typing import Any

# Data de referencia da captura. Escolhida como o ultimo dia com faturamento na
# base entregue (outubro/2026 tem 4 notas), garantindo que periodos em andamento
# e ciclos fechados tenham dados dos dois lados.
DATA_REFERENCIA = date(2026, 10, 1)


def _make_frozen_date(hoje: date) -> type[date]:
    """Cria uma subclasse de ``date`` cujo ``today()`` retorna uma data fixa.

    Args:
        hoje: Data que ``today()`` deve devolver.

    Returns:
        Subclasse de ``datetime.date`` pronta para substituir o atributo de modulo.
    """

    class _FrozenDate(date):
        @classmethod
        def today(cls) -> date:
            return hoje

    return _FrozenDate


@contextlib.contextmanager
def congelar_hoje(modulos: list[Any], hoje: date = DATA_REFERENCIA) -> Iterator[date]:
    """Congela ``date.today()`` nos modulos informados durante o bloco.

    Args:
        modulos: Modulos cujo atributo ``date`` deve ser substituido.
        hoje: Data fixa a ser devolvida por ``date.today()``.

    Yields:
        A data congelada, para uso nas chamadas da captura.
    """
    frozen = _make_frozen_date(hoje)
    originais: list[tuple[Any, Any]] = []
    for mod in modulos:
        if hasattr(mod, "date"):
            originais.append((mod, mod.date))
            mod.date = frozen
    try:
        yield hoje
    finally:
        for mod, original in originais:
            mod.date = original
