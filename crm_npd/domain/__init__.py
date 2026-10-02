"""Linguagem de negocio do CRM NPD como tipos.

Esta camada nao conhece banco, web nem arquivo. Ela existe para que as formulas
em :mod:`crm_npd.metrics` recebam e devolvam objetos com nome e contrato, em vez
de dicionarios soltos e ``sqlite3.Row``.
"""

from crm_npd.domain.enums import (
    ClassificacaoCliente,
    EtapaFunil,
    FaixaScore,
    PrioridadeTarefa,
    StatusManualCliente,
    TipoVendedor,
)
from crm_npd.domain.valores import (
    DimensoesScoreVendedor,
    EstatisticasIdentidade,
    ExtratoComissao,
    LinhaComissao,
    MetricasPipeline,
    PainelMeta,
    Periodo,
    ScoreIdentidade,
)

__all__ = [
    "ClassificacaoCliente",
    "DimensoesScoreVendedor",
    "EstatisticasIdentidade",
    "EtapaFunil",
    "ExtratoComissao",
    "FaixaScore",
    "LinhaComissao",
    "MetricasPipeline",
    "PainelMeta",
    "Periodo",
    "PrioridadeTarefa",
    "ScoreIdentidade",
    "StatusManualCliente",
    "TipoVendedor",
]
