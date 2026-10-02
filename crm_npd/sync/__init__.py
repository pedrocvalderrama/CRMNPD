"""ETL do ERP Sisplan para o SQLite do CRM.

Roda em paralelo a importacao CSV. A promocao a fonte primaria e a Fase 5 e
depende de o reconcile casar centavo a centavo com os totais de controle.
"""

from crm_npd.sync.etl import sincronizar_periodo
from crm_npd.sync.reconcile import reconciliar_faturamento

__all__ = ["sincronizar_periodo", "reconciliar_faturamento"]
