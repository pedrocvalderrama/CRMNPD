"""Extrai blocos do CRM 10.10 para o projeto organizado. Uso unico de port."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "CRM OUTRO" / "CRM 10.10" / "crm_npd" / "legacy_monolith.py"
lines = SRC.read_text(encoding="utf-8").splitlines()


def sl(a: int, b: int) -> str:
    return "\n".join(lines[a - 1 : b])


HEADER_REL = '''"""Helpers de relatorios, A Faturar e Excel portados do CRM 10.10.

Leem o SQLite do CRM. A origem da carga continua sendo o Firebird (SELECT-only).
"""
from __future__ import annotations

import io
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, List, Optional

from flask import send_file


def _legacy():
    from crm_npd import legacy_monolith as m
    return m


MONTHS_PT = [
    (1, "Janeiro"), (2, "Fevereiro"), (3, "Março"), (4, "Abril"),
    (5, "Maio"), (6, "Junho"), (7, "Julho"), (8, "Agosto"),
    (9, "Setembro"), (10, "Outubro"), (11, "Novembro"), (12, "Dezembro"),
]
MONTH_NAME_PT = dict(MONTHS_PT)


def format_num_br(v: float, decimals: int = 0) -> str:
    try:
        s = f"{float(v):,.{int(decimals)}f}"
        return s.replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(v)


'''

# Replace globals that pointed to 10.10 names.
repls = {
    "_MONTH_NAME_PT": "MONTH_NAME_PT",
    "_MONTHS_PT": "MONTHS_PT",
    "sql_pedidos_realizados_excel_cond(": "_legacy().sql_pedidos_realizados_excel_cond(",
    "sql_pedido_cancelado_cond(": "_legacy().sql_pedido_cancelado_cond(",
    "periodo_mes_calendario(": "_legacy().periodo_mes_calendario(",
    "periodo_ciclo_25_24_que_contem_hoje(": "_legacy().periodo_ciclo_25_24_que_contem_hoje(",
    "norm(": "_legacy().norm(",
}


def apply(txt: str) -> str:
    for a, b in repls.items():
        txt = txt.replace(a, b)
    txt = txt.replace("if '_legacy().norm(' in globals() else", "if False else")
    txt = txt.replace(
        "return d0, d1, f\"{MONTH_NAME_PT.get(ref.month, ref.month)}/{ref.year}\" if '_legacy().norm(' in globals() else ref.strftime('%m/%Y')",
        "return d0, d1, f\"{MONTH_NAME_PT.get(ref.month, ref.month)}/{ref.year}\"",
    )
    txt = txt.replace(
        'return f"CAST(strftime(\'%m\',{col}) AS INTEGER) = ?", (ref.month,), MONTH_NAME_PT.get(ref.month, str(ref.month)) if \'_legacy().norm(\' in globals() else ref.strftime(\'%m\')',
        'return f"CAST(strftime(\'%m\',{col}) AS INTEGER) = ?", (ref.month,), MONTH_NAME_PT.get(ref.month, str(ref.month))',
    )
    txt = txt.replace(
        '"label": "Reimporte o 721 para carregar ENTREGA_PEDIDO"',
        '"label": "Atualize o Sisplan para carregar a data de entrega"',
    )
    return txt


rel_body = apply(
    sl(7705, 8065)
    + "\n\n"
    + sl(18687, 18718)
    + "\n\n"
    + sl(19348, 19732)
)

(ROOT / "crm_npd" / "services" / "relatorios.py").write_text(HEADER_REL + rel_body + "\n", encoding="utf-8")

HEADER_PLAN = '''"""Planejamento sazonal / forecast / consumo de papel (CRM 10.10)."""
from __future__ import annotations

import calendar
import re
import statistics
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from crm_npd.services.relatorios import (
    MONTH_NAME_PT,
    MONTHS_PT,
    _analysis_scope_sql,
    _legacy,
    _report_datasets,
    _report_months_label,
    _report_selected_months,
    _resolve_report_period,
)

'''

plan_body = apply(sl(18720, 19198))
(ROOT / "crm_npd" / "services" / "planejamento.py").write_text(HEADER_PLAN + plan_body + "\n", encoding="utf-8")

HEADER_VEND = '''"""Ciclo de vida de vendedores (ativos / inativos) portado do CRM 10.10."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from crm_npd.services.relatorios import _legacy


'''

vend_body = apply(sl(18411, 18533))
# _vendor_team_alias_key uses _norm_jit_text_py
vend_body = vend_body.replace("_norm_jit_text_py(", "_legacy()._norm_jit_text_py(")
vend_body = vend_body.replace("ensure_vendors_schema(", "_legacy().ensure_vendors_schema(")
vend_body = vend_body.replace("now_iso()", "_legacy().now_iso()")
vend_body = vend_body.replace("sql_pedidos_realizados_excel_cond(", "_legacy().sql_pedidos_realizados_excel_cond(")
(ROOT / "crm_npd" / "services" / "vendedores_ciclo.py").write_text(HEADER_VEND + vend_body + "\n", encoding="utf-8")

print("helpers ok", len(rel_body), len(plan_body), len(vend_body))
