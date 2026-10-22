"""ETL do ERP Sisplan para o SQLite do CRM.

A fonte comercial e o FDB. ``sincronizar_crm`` substitui pedidos, notas,
orcamentos e liquidez. No servidor, o ouvinte CDC (`sync.cdc`) regrava
so as chaves que mudaram nos logs do Sisplan.
"""

from crm_npd.sync.cdc import ciclo, iniciar_cdc_em_background, loop_cdc
from crm_npd.sync.etl import sincronizar_crm, sincronizar_faturamento, sincronizar_periodo
from crm_npd.sync.fonte import fonte_primaria, usando_firebird
from crm_npd.sync.reconcile import reconciliar_faturamento

__all__ = [
    "ciclo",
    "iniciar_cdc_em_background",
    "loop_cdc",
    "sincronizar_crm",
    "sincronizar_faturamento",
    "sincronizar_periodo",
    "fonte_primaria",
    "usando_firebird",
    "reconciliar_faturamento",
]
