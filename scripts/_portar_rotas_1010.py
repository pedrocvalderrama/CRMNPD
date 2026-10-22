"""Gera crm_npd/web/relatorios.py a partir das rotas do CRM 10.10."""
from __future__ import annotations

from pathlib import Path
from textwrap import indent

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "CRM OUTRO" / "CRM 10.10" / "crm_npd" / "legacy_monolith.py"
lines = SRC.read_text(encoding="utf-8").splitlines()


def sl(a: int, b: int) -> str:
    return "\n".join(lines[a - 1 : b])


header = '''"""Rotas de relatorios, A Faturar, analise historica e vendedores inativos."""
from __future__ import annotations


def registrar_rotas(m) -> None:
    """Registra as telas novas no app Flask do monolito."""
    from datetime import date, datetime, timedelta
    import json

    from flask import abort, flash, redirect, render_template_string, request, url_for

    from crm_npd.services import planejamento as P
    from crm_npd.services import relatorios as R
    from crm_npd.services import vendedores_ciclo as V

    app = m.app
    login_required = m.login_required
    conectar = m.conectar
    render_page = m.render_page
    get_current_user = m.get_current_user
    get_setting = m.get_setting
    format_brl = m.format_brl
    bar_chart = m.bar_chart
    create_task_unificada = m.create_task_unificada
    now_iso = m.now_iso
    ensure_vendors_schema = m.ensure_vendors_schema
    sql_pedidos_realizados_excel_cond = m.sql_pedidos_realizados_excel_cond
    sql_pedido_cancelado_cond = m.sql_pedido_cancelado_cond
    format_num_br = R.format_num_br

    _parse_report_ref = R._parse_report_ref
    _resolve_report_period = R._resolve_report_period
    _report_datasets = R._report_datasets
    _report_selected_months = R._report_selected_months
    _report_months_label = R._report_months_label
    _report_apply_dataset_filters = R._report_apply_dataset_filters
    _xlsx_response = R._xlsx_response
    _a_faturar_data = R._a_faturar_data
    _analysis_scope_sql = R._analysis_scope_sql
    _analysis_scope_label = R._analysis_scope_label
    _MONTHS_PT = R.MONTHS_PT
    _MONTH_NAME_PT = R.MONTH_NAME_PT
    _planning_analysis_data = P._planning_analysis_data
    _sync_vendedores_inativos = V._sync_vendedores_inativos
    _vendor_team_alias_key = V._vendor_team_alias_key
    _vendor_alias_ids = V._vendor_alias_ids
    _vendor_cache_clear = V._vendor_cache_clear

'''

body = "\n\n".join(
    [
        sl(12633, 12673),
        sl(18536, 18575),
        sl(19200, 19343),
        sl(19735, 20356),
    ]
)
body = body.replace("pedidos_parser_ok", "entrega_ok")
body = body.replace(
    "parser_ok = bool(get_setting(conn, \"pedidos_parser_v511_ok\", \"\"))",
    "parser_ok = True",
)
body = body.replace(
    "parser_atualizado = bool(get_setting(conn, \"pedidos_parser_v511_ok\", \"\"))",
    "parser_atualizado = True",
)
# cobertura de entrega no lugar do parser CSV
old_parser_assign = "entrega_ok=bool(get_setting(conn, 'pedidos_parser_v511_ok', ''))"
if old_parser_assign not in body:
    body = body.replace(
        "entrega_ok=bool(get_setting(conn, 'pedidos_parser_v511_ok', ''))",
        "entrega_ok=True",
    )
body = body.replace(
    "entrega_ok=bool(get_setting(conn, 'pedidos_parser_v511_ok', ''))",
    "entrega_ok = True",
)

# a_faturar original assignment
body = body.replace(
    "entrega_ok=bool(get_setting(conn, 'pedidos_parser_v511_ok', ''))",
    "entrega_ok=True",
)

out = header + indent(body, "    ") + "\n"
dest = ROOT / "crm_npd" / "web" / "relatorios.py"
dest.write_text(out, encoding="utf-8")
print("wrote", dest, "chars", len(out))
