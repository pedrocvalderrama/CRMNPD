"""Helpers de relatorios, A Faturar e Excel portados do CRM 10.10.

Leem o SQLite do CRM. A origem da carga continua sendo o Firebird (SELECT-only).
"""
from __future__ import annotations

import io
import re
import sqlite3
from collections import defaultdict
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


def _parse_report_ref(value: str | None, default: date | None = None) -> date:
    """Aceita YYYY-MM-DD ou YYYY-MM e devolve uma data de referência segura."""
    default = default or date.today()
    txt = (value or "").strip()
    if not txt:
        return default
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            d = datetime.strptime(txt, fmt).date()
            return d if fmt == "%Y-%m-%d" else d.replace(day=1)
        except Exception:
            pass
    return default


def _resolve_report_period(periodo: str, ref: date) -> tuple[date, date, str]:
    """Grade única de períodos usada por Dashboard, Relatórios e Excel."""
    p = (periodo or "mes").strip().lower()
    if p == "dia":
        return ref, ref, ref.strftime("%d/%m/%Y")
    if p == "semana":
        d0 = ref - timedelta(days=ref.weekday())
        d1 = d0 + timedelta(days=6)
        return d0, d1, f"Semana {d0.strftime('%d/%m')}–{d1.strftime('%d/%m/%Y')}"
    if p == "trimestre":
        q = (ref.month - 1) // 3 + 1
        m0 = (q - 1) * 3 + 1
        d0 = date(ref.year, m0, 1)
        d1 = (date(ref.year + (1 if m0 + 3 > 12 else 0), ((m0 + 2) % 12) + 1, 1) - timedelta(days=1))
        return d0, d1, f"{q}º trimestre/{ref.year}"
    if p == "semestre":
        sem = 1 if ref.month <= 6 else 2
        m0 = 1 if sem == 1 else 7
        d0 = date(ref.year, m0, 1)
        d1 = date(ref.year, 6 if sem == 1 else 12, 30 if sem == 1 else 31)
        return d0, d1, f"{sem}º semestre/{ref.year}"
    if p == "ano":
        return date(ref.year, 1, 1), date(ref.year, 12, 31), str(ref.year)
    d0, d1 = _legacy().periodo_mes_calendario(ref.replace(day=1))
    return d0, d1, f"{MONTH_NAME_PT.get(ref.month, ref.month)}/{ref.year}" if 'MONTH_NAME_PT' in globals() else ref.strftime('%m/%Y')


def _dashboard_bounds(periodo: str, ref: date) -> tuple[date, date, date, date, str]:
    """No mês preserva a regra oficial: pedidos mês civil e faturamento no ciclo 26→25.
    Dia/semana usam exatamente o mesmo recorte para os dois lados, como filtro de planilha.
    """
    p = (periodo or "mes").strip().lower()
    if p == "mes":
        p0, p1 = _legacy().periodo_mes_calendario(ref.replace(day=1))
        f0, f1 = _legacy().periodo_ciclo_25_24_que_contem_hoje(ref.replace(day=1))
        return p0, p1, f0, f1, f"Mês {ref.strftime('%m/%Y')}"
    d0, d1, label = _resolve_report_period(p, ref)
    return d0, d1, d0, d1, label


def _historical_period_selector(periodo: str, ref: date, alias: str) -> tuple[str, tuple, str]:
    """Filtro sazonal sem ano: permite comparar o mesmo dia/semana/mês/trimestre/semestre entre anos."""
    p = (periodo or "mes").strip().lower()
    col = f"{alias}.data_emissao"
    if p == "dia":
        return f"strftime('%m-%d',{col}) = ?", (ref.strftime('%m-%d'),), ref.strftime('%d/%m')
    if p == "semana":
        week = int(ref.strftime('%W'))
        return f"CAST(strftime('%W',{col}) AS INTEGER) = ?", (week,), f"semana {week:02d}"
    if p == "trimestre":
        q = (ref.month - 1) // 3 + 1
        m0, m1 = (q - 1) * 3 + 1, q * 3
        return f"CAST(strftime('%m',{col}) AS INTEGER) BETWEEN ? AND ?", (m0, m1), f"{q}º trimestre"
    if p == "semestre":
        sem = 1 if ref.month <= 6 else 2
        m0, m1 = (1, 6) if sem == 1 else (7, 12)
        return f"CAST(strftime('%m',{col}) AS INTEGER) BETWEEN ? AND ?", (m0, m1), f"{sem}º semestre"
    if p == "ano":
        return "1=1", tuple(), "ano completo"
    return f"CAST(strftime('%m',{col}) AS INTEGER) = ?", (ref.month,), MONTH_NAME_PT.get(ref.month, str(ref.month)) if 'MONTH_NAME_PT' in globals() else ref.strftime('%m')


def _safe_excel_name(txt: str) -> str:
    txt = re.sub(r"[^A-Za-z0-9_-]+", "_", txt or "relatorio").strip("_")
    return (txt or "relatorio")[:80]


def linhas_excel_extrato_comissao(data_fin: dict) -> list[list]:
    """Monta as linhas do Excel do extrato de comissão, na mesma ordem da tela."""
    rows: list[list] = []
    for linha in data_fin.get("lines") or []:
        is_dev = bool(linha.get("is_devolucao"))
        if is_dev:
            status = "DEVOLUÇÃO"
            aliquota = None
            comissao = None
        else:
            status = "NOVO" if linha.get("is_novo") else "CARTEIRA"
            aliquota = (float(linha.get("base_pct") or 0) + float(linha.get("bonus_pct") or 0)) / 100.0
            comissao = float(linha.get("comissao") or 0)
        rows.append([
            linha.get("data") or "",
            linha.get("doc_ref") or "",
            linha.get("pedido") or "",
            linha.get("cliente") or "",
            linha.get("identity_nome") or "",
            status,
            "SIM" if linha.get("is_jit") else "",
            float(linha.get("valor") or 0),
            aliquota,
            comissao,
            "SIM" if linha.get("is_anfarma") else "",
        ])
    extra = float(data_fin.get("extra_bonus") or 0)
    if extra:
        rows.append([
            "", "", "", "Bônus 1% sobre o excedente de R$ 100 mil", "", "", "",
            None, None, extra,
        ])
    return rows


def _xlsx_clean_value(value):
    """Converte valores para tipos seguros no XML do XLSX e remove controles inválidos."""
    if value is None or isinstance(value, (int, float, bool, date, datetime)):
        return value
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="replace")
        except Exception:
            value = str(value)
    if not isinstance(value, str):
        value = str(value)
    # XML 1.0 não aceita caracteres de controle (exceto tab, LF e CR).
    return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", value)


def _xlsx_response(filename: str, title: str, metadata: list[tuple[str, Any]], sheets: list[dict]):
    """Gera XLSX legível, horizontal e com tabela/auto-filtro em cada aba."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
        from openpyxl.utils import get_column_letter
        from openpyxl.chart import BarChart, LineChart, Reference
    except Exception as exc:
        raise RuntimeError("openpyxl não está disponível. Execute ATUALIZAR_DEPENDENCIAS.bat.") from exc

    wb = Workbook()
    ws = wb.active
    ws.title = "Resumo"
    navy = "102A43"; blue = "2563EB"; cyan = "06B6D4"; light = "EAF1F8"; line = "D9E2EC"
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(6, len(metadata)))
    ws.cell(1, 1, title)
    ws.cell(1, 1).font = Font(size=16, bold=True, color="FFFFFF")
    ws.cell(1, 1).fill = PatternFill("solid", fgColor=navy)
    ws.cell(1, 1).alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 28
    for idx, (label, value) in enumerate(metadata, start=1):
        ws.cell(3, idx, label)
        value = _xlsx_clean_value(value)
        ws.cell(4, idx, value)
        ws.cell(3, idx).font = Font(bold=True, color="FFFFFF")
        ws.cell(3, idx).fill = PatternFill("solid", fgColor=blue)
        ws.cell(3, idx).alignment = Alignment(horizontal="center")
        ws.cell(4, idx).fill = PatternFill("solid", fgColor="F7FAFC")
        ws.cell(4, idx).alignment = Alignment(horizontal="center")
        lab_up = str(label).upper()
        if isinstance(value, (int, float)):
            if any(k in lab_up for k in ["FORECAST", "GAP", "VALOR", "FATUR", "PEDIDO R$", "RECEITA"]):
                ws.cell(4, idx).number_format = 'R$ #,##0.00'
            elif "KG" in lab_up:
                ws.cell(4, idx).number_format = '#,##0.0 "kg"'
            else:
                ws.cell(4, idx).number_format = '#,##0.00'
        ws.column_dimensions[get_column_letter(idx)].width = max(14, min(28, len(str(label)) + 4, len(str(value)) + 4))
    ws.freeze_panes = "A5"

    for spec in sheets:
        name = str(spec.get("name") or "Dados")[:31]
        sh = wb.create_sheet(name)
        headers = [str(x) for x in (spec.get("headers") or [])]
        rows = spec.get("rows") or []
        sh.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(1, len(headers)))
        sh.cell(1, 1, spec.get("title") or name)
        sh.cell(1, 1).font = Font(size=14, bold=True, color="FFFFFF")
        sh.cell(1, 1).fill = PatternFill("solid", fgColor=navy)
        sh.row_dimensions[1].height = 25
        for c, h in enumerate(headers, start=1):
            cell = sh.cell(3, c, h)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor=blue if c % 2 else cyan)
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for r_idx, row in enumerate(rows, start=4):
            for c_idx, val in enumerate(row, start=1):
                val = _xlsx_clean_value(val)
                cell = sh.cell(r_idx, c_idx, val)
                if r_idx % 2 == 0:
                    cell.fill = PatternFill("solid", fgColor="F7FAFC")
                cell.border = Border(bottom=Side(style="hair", color=line))
                cell.alignment = Alignment(vertical="top")
                h = headers[c_idx - 1].upper() if c_idx - 1 < len(headers) else ""
                if isinstance(val, (int, float)):
                    if "VALOR" in h or "TICKET" in h or "META" in h:
                        cell.number_format = 'R$ #,##0.00'
                    elif "%" in h or "PERCENT" in h or "TAXA" in h:
                        cell.number_format = '0.00%'
                    elif "QUANT" in h or "QTD" in h or "PEDIDOS" in h or "NFS" in h or "CLIENTES" in h:
                        cell.number_format = '#,##0.00' if isinstance(val, float) and not float(val).is_integer() else '#,##0'
        if headers:
            sh.freeze_panes = "A4"
            last_col = get_column_letter(len(headers))
            last_row = max(3, 3 + len(rows))
            # Um único AutoFilter simples. Evitamos objetos Table aqui porque algumas
            # versões do Excel tentam reparar arquivos que combinam Table + AutoFilter
            # no mesmo intervalo, principalmente quando o relatório é montado dinamicamente.
            sh.auto_filter.ref = f"A3:{last_col}{last_row}"
            for c_idx, h in enumerate(headers, start=1):
                vals = [str(r[c_idx-1]) if c_idx-1 < len(r) and r[c_idx-1] is not None else "" for r in rows[:200]]
                width = min(42, max(10, len(h) + 2, max([len(v) for v in vals], default=0) + 2))
                sh.column_dimensions[get_column_letter(c_idx)].width = width

        # Gráficos opcionais: ficam à direita da tabela para manter leitura horizontal.
        chart_spec = spec.get("chart") or {}
        if rows and chart_spec and headers:
            try:
                ctype = str(chart_spec.get("type") or "bar").lower()
                chart = LineChart() if ctype == "line" else BarChart()
                if ctype != "line":
                    chart.type = "col"
                chart.style = 10
                chart.title = str(chart_spec.get("title") or spec.get("title") or name)
                chart.y_axis.title = str(chart_spec.get("y_title") or "")
                chart.x_axis.title = str(chart_spec.get("x_title") or "")
                cat_col = int(chart_spec.get("category_col") or 1)
                val_cols = chart_spec.get("value_cols") or [int(chart_spec.get("value_col") or 2)]
                cats = Reference(sh, min_col=cat_col, min_row=4, max_row=3+len(rows))
                for vc in val_cols:
                    data = Reference(sh, min_col=int(vc), min_row=3, max_row=3+len(rows))
                    chart.add_data(data, titles_from_data=True)
                chart.set_categories(cats)
                chart.height = 8
                chart.width = 15
                sh.add_chart(chart, f"{get_column_letter(len(headers)+2)}3")
            except Exception:
                pass
    bio = io.BytesIO()
    wb.save(bio)

    # Validação antes de entregar: ZIP íntegro, XML parseável e workbook reaberto
    # pelo próprio engine. Se algo estiver estruturalmente errado, o usuário recebe
    # erro no CRM em vez de um Excel que pede "Reparar".
    raw = bio.getvalue()
    try:
        import zipfile
        import xml.etree.ElementTree as ET
        from openpyxl import load_workbook
        with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
            bad = zf.testzip()
            if bad:
                raise ValueError(f"Entrada corrompida no XLSX: {bad}")
            for member in zf.namelist():
                if member.endswith((".xml", ".rels")):
                    ET.fromstring(zf.read(member))
        chk = load_workbook(io.BytesIO(raw), read_only=True, data_only=False)
        chk.close()
    except Exception as exc:
        raise RuntimeError(f"Falha ao validar o Excel gerado: {exc}") from exc

    bio = io.BytesIO(raw)
    bio.seek(0)
    return send_file(bio, as_attachment=True, download_name=f"{_safe_excel_name(filename)}.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _report_scope_sql(scope: str, alias: str = "v") -> str:
    s = (scope or "TOTAL").strip().upper()
    tipo = f"COALESCE(UPPER(TRIM({alias}.tipo)),'EXTERNO')"
    nome = f"COALESCE(UPPER(TRIM({alias}.nome)),'')"
    if s == "INTERNO": return f"({tipo}='INTERNO' AND {nome}<>'SITE')"
    if s == "EXTERNO": return f"({tipo}='EXTERNO' AND {nome}<>'SITE')"
    if s == "SITE": return f"({tipo}='SITE' OR {nome}='SITE')"
    return "1=1"


def _report_datasets(conn: sqlite3.Connection, d0: date, d1: date, scope: str = "TOTAL") -> dict:
    scope_sql = _report_scope_sql(scope, "v")
    orders = [dict(r) for r in conn.execute(f"""
        SELECT date(o.data_emissao) AS data, TRIM(COALESCE(o.pedido,'')) AS pedido,
               COALESCE(NULLIF(TRIM(c.fantasia),''),NULLIF(TRIM(c.nome),''),NULLIF(TRIM(o.cliente_nome_ref),''),'—') AS cliente,
               COALESCE(NULLIF(TRIM(v.nome),''),NULLIF(TRIM(o.vendor_nome_ref),''),'—') AS vendedor,
               CASE WHEN COALESCE(UPPER(TRIM(v.nome)),'')='SITE' OR COALESCE(UPPER(TRIM(v.tipo)),'')='SITE' THEN 'SITE' ELSE COALESCE(UPPER(TRIM(v.tipo)),'EXTERNO') END AS time,
               COALESCE(o.grupo_ref,'') AS grupo, COALESCE(o.medida_ref,'') AS medida, COALESCE(o.papel_ref,'') AS papel,
               COALESCE(o.quantidade,0) AS quantidade, COALESCE(NULLIF(o.valor_item_ref,0),o.valor,0) AS valor
        FROM orders o LEFT JOIN customers c ON c.id=o.cliente_id LEFT JOIN vendors v ON v.id=o.vendor_id
        WHERE date(o.data_emissao) BETWEEN date(?) AND date(?) AND {scope_sql}
          AND {_legacy().sql_pedidos_realizados_excel_cond('o','v')}
        ORDER BY date(o.data_emissao), pedido, o.id
    """, (d0.isoformat(), d1.isoformat())).fetchall()]
    invoices = [dict(r) for r in conn.execute(f"""
        SELECT date(i.data_emissao) AS data, COALESCE(i.numero,'') AS nf, COALESCE(i.pedido,'') AS pedido,
               COALESCE(NULLIF(TRIM(c.fantasia),''),NULLIF(TRIM(c.nome),''),NULLIF(TRIM(i.cliente_nome_ref),''),'—') AS cliente,
               COALESCE(NULLIF(TRIM(v.nome),''),NULLIF(TRIM(i.vendor_nome_ref),''),'—') AS vendedor,
               CASE WHEN COALESCE(UPPER(TRIM(v.nome)),'')='SITE' OR COALESCE(UPPER(TRIM(v.tipo)),'')='SITE' THEN 'SITE' ELSE COALESCE(UPPER(TRIM(v.tipo)),'EXTERNO') END AS time,
               COALESCE(i.grupo_ref,'') AS grupo, COALESCE(i.formato,i.medida_ref,'') AS formato, COALESCE(i.papel_ref,'') AS papel,
               COALESCE(i.produto,'') AS produto, COALESCE(i.quantidade,0) AS quantidade, COALESCE(i.valor,0) AS valor
        FROM invoices i LEFT JOIN customers c ON c.id=i.cliente_id LEFT JOIN vendors v ON v.id=i.vendor_id
        WHERE i.data_emissao IS NOT NULL AND date(i.data_emissao) BETWEEN date(?) AND date(?) AND {scope_sql}
        ORDER BY date(i.data_emissao), nf, i.id
    """, (d0.isoformat(), d1.isoformat())).fetchall()]
    p_orders = len({str(r.get('pedido') or '').strip() for r in orders if str(r.get('pedido') or '').strip()})
    p_value = sum(float(r.get('valor') or 0) for r in orders)
    p_qty = sum(float(r.get('quantidade') or 0) for r in orders)
    f_nfs = len({str(r.get('nf') or '').strip() for r in invoices if str(r.get('nf') or '').strip()})
    f_value = sum(float(r.get('valor') or 0) for r in invoices)
    f_qty = sum(float(r.get('quantidade') or 0) for r in invoices)
    return {"orders": orders, "invoices": invoices, "p_orders": p_orders, "p_value": p_value, "p_qty": p_qty, "f_nfs": f_nfs, "f_value": f_value, "f_qty": f_qty}


def _report_selected_months(args) -> List[int]:
    """Lê seleção de meses no estilo Excel (mes=1&mes=2...) ou meses=1,2,3."""
    vals = []
    try:
        vals.extend(args.getlist("mes"))
    except Exception:
        pass
    raw = (args.get("meses") or "") if hasattr(args, "get") else ""
    if raw:
        vals.extend(str(raw).split(","))
    out = []
    for v in vals:
        try:
            m = int(str(v).strip())
        except Exception:
            continue
        if 1 <= m <= 12 and m not in out:
            out.append(m)
    return sorted(out)


def _report_months_label(months: List[int], year: int) -> str:
    if not months:
        return str(year)
    names = [MONTH_NAME_PT.get(m, str(m))[:3] for m in months]
    return f"{' + '.join(names)}/{year}"


def _report_date_in_months(value: Any, year: int, months: List[int]) -> bool:
    if not months:
        return True
    try:
        d = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        return d.year == int(year) and d.month in set(months)
    except Exception:
        return False


def _report_recalc_dataset(ds: dict) -> dict:
    ds = dict(ds or {})
    orders = list(ds.get("orders") or [])
    invoices = list(ds.get("invoices") or [])
    ds["orders"] = orders
    ds["invoices"] = invoices
    ds["p_orders"] = len({str(r.get('pedido') or '').strip() for r in orders if str(r.get('pedido') or '').strip()})
    ds["p_value"] = sum(float(r.get('valor') or 0) for r in orders)
    ds["p_qty"] = sum(float(r.get('quantidade') or 0) for r in orders)
    ds["f_nfs"] = len({str(r.get('nf') or '').strip() for r in invoices if str(r.get('nf') or '').strip()})
    ds["f_value"] = sum(float(r.get('valor') or 0) for r in invoices)
    ds["f_qty"] = sum(float(r.get('quantidade') or 0) for r in invoices)
    return ds


def _report_apply_dataset_filters(ds: dict, *, year: Optional[int] = None, months: Optional[List[int]] = None,
                                  vendedor: str = "", cliente: str = "", grupo: str = "",
                                  papel: str = "", medida: str = "") -> dict:
    """Aplica os filtros avançados compartilhados pelos relatórios comerciais."""
    months = months or []
    vendedor_n = _legacy().norm(vendedor) if vendedor else ""
    cliente_n = _legacy().norm(cliente) if cliente else ""
    grupo_n = _legacy().norm(grupo) if grupo else ""
    papel_n = _legacy().norm(papel) if papel else ""
    medida_n = _legacy().norm(medida) if medida else ""

    def ok(r, is_invoice=False):
        if year is not None and months and not _report_date_in_months(r.get('data'), year, months):
            return False
        if vendedor_n and vendedor_n not in _legacy().norm(r.get('vendedor') or ''):
            return False
        if cliente_n and cliente_n not in _legacy().norm(r.get('cliente') or ''):
            return False
        if grupo_n and grupo_n not in _legacy().norm(r.get('grupo') or ''):
            return False
        if papel_n and papel_n not in _legacy().norm(r.get('papel') or ''):
            return False
        rmed = r.get('formato') if is_invoice else r.get('medida')
        if medida_n and medida_n not in _legacy().norm(rmed or ''):
            return False
        return True

    ds = dict(ds or {})
    ds['orders'] = [r for r in (ds.get('orders') or []) if ok(r, False)]
    ds['invoices'] = [r for r in (ds.get('invoices') or []) if ok(r, True)]
    return _report_recalc_dataset(ds)

# V4.6.0 — DETALHAMENTO DO DASHBOARD + ANÁLISE HISTÓRICA / PREVISÃO
# -------------------------------------------------------------------------
MONTHS_PT = [
    (1, "Janeiro"), (2, "Fevereiro"), (3, "Março"), (4, "Abril"),
    (5, "Maio"), (6, "Junho"), (7, "Julho"), (8, "Agosto"),
    (9, "Setembro"), (10, "Outubro"), (11, "Novembro"), (12, "Dezembro"),
]
MONTH_NAME_PT = dict(MONTHS_PT)


def _analysis_scope_sql(scope: str, vendor_alias: str = "v") -> str:
    """Filtro de equipe com a mesma classificação usada no Dashboard."""
    s = (scope or "TOTAL").strip().upper()
    v = vendor_alias
    tipo = f"COALESCE(UPPER(TRIM({v}.tipo)), 'EXTERNO')"
    nome = f"COALESCE(UPPER(TRIM({v}.nome)), '')"
    if s == "INTERNO":
        return f"({tipo}='INTERNO' AND {nome}<>'SITE')"
    if s == "EXTERNO":
        return f"({tipo}='EXTERNO' AND {nome}<>'SITE')"
    if s == "SITE":
        return f"({tipo}='SITE' OR {nome}='SITE')"
    return "1=1"


def _analysis_scope_label(scope: str) -> str:
    return {
        "TOTAL": "Empresa inteira",
        "INTERNO": "Time interno",
        "EXTERNO": "Time externo",
        "SITE": "Site / Marketplace",
    }.get((scope or "TOTAL").upper(), "Empresa inteira")

def _pedido_match_key(value: Any) -> str:
    """Normaliza nº de pedido preservando subpedido (/01, /02...).

    A versão anterior removia tudo após ``/`` e podia fundir pedidos distintos
    como 00340/01 e 00340/02. Agora normalizamos zeros à esquerda, mas mantemos
    o sufixo. Quando 731/733 usa apenas o número-base, a conciliação faz fallback
    somente se existir um único pedido 721 possível para aquela base.
    """
    txt = _legacy().norm(str(value or "")).strip().upper()
    if not txt:
        return ""
    if re.fullmatch(r"\d+\.0+", txt):
        txt = txt.split(".", 1)[0]
    if "/" in txt:
        base, suffix = txt.split("/", 1)
        base = base.strip()
        suffix = suffix.strip()
        if re.fullmatch(r"\d+", base):
            base = base.lstrip("0") or "0"
        if re.fullmatch(r"\d+", suffix):
            suffix = suffix.lstrip("0") or "0"
        return f"{base}/{suffix}" if suffix else base
    if re.fullmatch(r"\d+", txt):
        txt = txt.lstrip("0") or "0"
    return txt


def _pedido_base_key(value: Any) -> str:
    key = _pedido_match_key(value)
    return key.split("/", 1)[0] if "/" in key else key


def _add_business_days(start: date, days: int) -> date:
    """Soma dias úteis simples (segunda a sexta) para previsão operacional."""
    cur = start
    added = 0
    days = max(0, int(days or 0))
    while added < days:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            added += 1
    return cur


def _format_date_br(value: Optional[date]) -> str:
    return value.strftime("%d/%m/%Y") if value else ""


def _prever_entrega_721(item: dict) -> dict:
    """Resolve a melhor previsão de entrega usando somente regras conhecidas do 721.

    Prioridade:
    1) ENTREGA_PEDIDO exata;
    2) condição "EM ATÉ N DIAS ÚTEIS" + EMISSAO_PEDIDO;
    3) condições imediatas/JIT imediato;
    4) condição textual sem inventar uma data inexistente.
    """
    raw_exact = str(item.get("entrega_pedido") or "").strip()
    emissao_raw = str(item.get("data_emissao") or "").strip()
    cond_raw = str(item.get("condicao_entrega") or "").strip()
    cond = _legacy().norm(cond_raw).upper()

    exact = None
    emissao = None
    try:
        exact = datetime.strptime(raw_exact[:10], "%Y-%m-%d").date() if raw_exact else None
    except Exception:
        exact = None
    try:
        emissao = datetime.strptime(emissao_raw[:10], "%Y-%m-%d").date() if emissao_raw else None
    except Exception:
        emissao = None

    if exact:
        return {
            "date": exact,
            "iso": exact.isoformat(),
            "display": _format_date_br(exact),
            "source": "ENTREGA_PEDIDO",
            "label": "Data prevista do 721",
            "stock": False,
        }

    cond_compact = re.sub(r"\s+", " ", cond).strip()
    m = re.search(r"(?:EM\s+ATE|ATE)\s+(\d+)\s+DIAS?\s+UTEIS", cond_compact)
    if emissao and m:
        dias = int(m.group(1))
        due = _add_business_days(emissao, dias)
        return {
            "date": due,
            "iso": due.isoformat(),
            "display": _format_date_br(due),
            "source": "CONDICAO_ENTREGA",
            "label": f"Previsto: {dias} dias úteis após emissão",
            "stock": False,
        }

    # 05 - JIT IMEDIATO: operacionalmente precisa existir em estoque.
    if "JIT" in cond_compact and "IMEDIAT" in cond_compact:
        return {
            "date": emissao,
            "iso": emissao.isoformat() if emissao else "",
            "display": "Estoque / imediato",
            "source": "CONDICAO_ENTREGA",
            "label": "JIT imediato — requer estoque",
            "stock": True,
        }

    # 03 - IMEDIATA: prazo operacional imediato; usa a emissão como referência.
    if "IMEDIAT" in cond_compact:
        return {
            "date": emissao,
            "iso": emissao.isoformat() if emissao else "",
            "display": _format_date_br(emissao) if emissao else "Imediata",
            "source": "CONDICAO_ENTREGA",
            "label": "Entrega imediata",
            "stock": False,
        }

    if "ENTREGAR SOMENTE NO DIA" in cond_compact:
        return {
            "date": None,
            "iso": "",
            "display": "Data definida no 721",
            "source": "CONDICAO_ENTREGA",
            "label": "Atualize o Sisplan para carregar a data de entrega",
            "stock": False,
        }

    if cond_raw:
        return {
            "date": None,
            "iso": "",
            "display": cond_raw,
            "source": "CONDICAO_ENTREGA",
            "label": "Condição de entrega do 721",
            "stock": False,
        }

    return {"date": None, "iso": "", "display": "—", "source": "", "label": "Sem condição de entrega", "stock": False}

def _a_faturar_data(conn: sqlite3.Connection, *, scope: str = "TOTAL", vendedor: str = "", cliente: str = "",
                    produto: str = "", medida: str = "", status: str = "", entrega_ini: str = "",
                    entrega_fim: str = "", year: Optional[int] = None, months: Optional[List[int]] = None,
                    include_faturados: bool = False) -> dict:
    """Concilia PEDIDOS 721 x FATURAMENTO x LIQUIDEZ por número de pedido."""
    scope = (scope or "TOTAL").strip().upper()
    if scope not in {"TOTAL","INTERNO","EXTERNO","SITE"}: scope="TOTAL"
    scope_sql = _report_scope_sql(scope, "v")
    order_rows = [dict(r) for r in conn.execute(f"""
        SELECT o.id, TRIM(COALESCE(o.pedido,'')) pedido, date(o.data_emissao) data_emissao,
               date(o.entrega_pedido) entrega_pedido,
               o.cliente_id, o.vendor_id,
               COALESCE(NULLIF(TRIM(c.fantasia),''),NULLIF(TRIM(c.nome),''),NULLIF(TRIM(o.cliente_nome_ref),''),'—') cliente,
               COALESCE(NULLIF(TRIM(v.nome),''),NULLIF(TRIM(o.vendor_nome_ref),''),'—') vendedor,
               CASE WHEN UPPER(TRIM(COALESCE(v.nome,'')))='SITE' OR UPPER(TRIM(COALESCE(v.tipo,'')))='SITE' THEN 'SITE'
                    ELSE COALESCE(UPPER(TRIM(v.tipo)),'EXTERNO') END time,
               COALESCE(o.grupo_ref,'') grupo, COALESCE(o.tipo_item_ref,'') tipo_item,
               COALESCE(o.medida_ref,'') medida, COALESCE(o.papel_ref,'') papel,
               COALESCE(o.produto_codigo_ref,'') produto_codigo, COALESCE(o.produto_ref,'') produto,
               COALESCE(o.quantidade,0) quantidade, COALESCE(o.preco_ref,0) preco_unitario,
               COALESCE(o.valor_item_ref,0) valor_item, COALESCE(o.valor,0) valor_pedido,
               COALESCE(o.condicao_entrega,'') condicao_entrega, COALESCE(o.observacao_ref,'') observacao,
               COALESCE(o.situacao_pedido,'') situacao_pedido, COALESCE(o.pedido_cancelado,0) pedido_cancelado
        FROM orders o
        LEFT JOIN customers c ON c.id=o.cliente_id
        LEFT JOIN vendors v ON v.id=o.vendor_id
        WHERE TRIM(COALESCE(o.pedido,''))<>''
          AND {scope_sql}
        ORDER BY date(o.data_emissao), o.pedido, o.id
    """).fetchall()]

    inv_rows = [dict(r) for r in conn.execute("""
        SELECT TRIM(COALESCE(i.pedido,'')) pedido, COALESCE(i.numero,'') nf, date(i.data_emissao) data_emissao,
               COALESCE(i.valor,0) valor, COALESCE(i.quantidade,0) quantidade,
               COALESCE(i.produto,'') produto, COALESCE(i.formato,i.medida_ref,'') medida
        FROM invoices i WHERE TRIM(COALESCE(i.pedido,''))<>''
        ORDER BY date(i.data_emissao), i.numero, i.id
    """).fetchall()]
    liq_rows = [dict(r) for r in conn.execute("""
        SELECT TRIM(COALESCE(pedido,'')) pedido, date(data_pagto) data_pagto,
               COALESCE(NULLIF(valor_liq,0),valor_pago,0) valor
        FROM liquidez WHERE TRIM(COALESCE(pedido,''))<>''
        ORDER BY date(data_pagto), id
    """).fetchall()]

    # Primeiro indexa o 721. A conciliação 731/733 preserva subpedidos e só
    # usa o número-base quando ele aponta para um único pedido possível.
    ord_by=defaultdict(list)
    for r in order_rows:
        ord_by[_pedido_match_key(r.get('pedido'))].append(r)
    base_to_order_keys=defaultdict(list)
    for ok in ord_by.keys():
        if ok:
            base=_pedido_base_key(ok)
            if ok not in base_to_order_keys[base]:
                base_to_order_keys[base].append(ok)

    def _resolve_fact_order_key(raw_pedido):
        exact=_pedido_match_key(raw_pedido)
        if exact in ord_by:
            return exact
        base=_pedido_base_key(raw_pedido)
        candidates=base_to_order_keys.get(base,[])
        if len(candidates)==1:
            return candidates[0]
        return exact

    inv_by=defaultdict(list)
    for r in inv_rows:
        inv_by[_resolve_fact_order_key(r.get('pedido'))].append(r)
    liq_by=defaultdict(list)
    for r in liq_rows:
        liq_by[_resolve_fact_order_key(r.get('pedido'))].append(r)

    vendedor_n=_legacy().norm(vendedor) if vendedor else ''
    cliente_n=_legacy().norm(cliente) if cliente else ''
    produto_n=_legacy().norm(produto) if produto else ''
    medida_n=_legacy().norm(medida) if medida else ''
    status_n=(status or '').strip().upper()
    months=set(months or [])
    today=date.today()
    rows=[]; delivery_known=0; product_known=0

    for pedido, items in ord_by.items():
        if not pedido: continue
        base=items[0]
        pedido_display=str(base.get('pedido') or pedido).strip() or pedido
        item_value_sum=sum(float(x.get('valor_item') or 0) for x in items)
        # VALOR_PED (coluna U do 721) é o total do pedido e costuma se repetir
        # em cada item. Portanto nunca somamos esse campo entre as linhas: usamos
        # o maior valor informado. Só cai para a soma de VALOR_ITEM quando U vier vazio.
        order_totals=[float(x.get('valor_pedido') or 0) for x in items if float(x.get('valor_pedido') or 0) > 0.0001]
        ordered_value=max(order_totals) if order_totals else item_value_sum
        ordered_qty=sum(float(x.get('quantidade') or 0) for x in items)
        invs=inv_by.get(pedido,[]); liqs=liq_by.get(pedido,[])
        billed=sum(float(x.get('valor') or 0) for x in invs)
        billed_qty=sum(float(x.get('quantidade') or 0) for x in invs)
        paid=sum(float(x.get('valor') or 0) for x in liqs)
        remaining=max(ordered_value-billed,0.0)
        receivable=max(billed-paid,0.0)

        # O 721 é a fonte oficial para a situação operacional do pedido.
        # Não depende de vendedor ativo/performance: uma pendência continua sendo
        # pendência mesmo se o vendedor saiu do time. O 731 apenas concilia o valor.
        situacoes={_legacy().norm(str(x.get('situacao_pedido') or '')).upper() for x in items}
        cancelado_721=any(int(x.get('pedido_cancelado') or 0)==1 or 'CANCEL' in _legacy().norm(str(x.get('situacao_pedido') or '')).upper() for x in items)
        faturado_721=bool(situacoes) and all(('FATUR' in st) for st in situacoes if st)
        pendente_721=any(('PEND' in st) for st in situacoes)
        situacao_721='Cancelado' if cancelado_721 else ('Pendente' if pendente_721 else ('Faturado' if faturado_721 else next((str(x.get('situacao_pedido') or '').strip() for x in items if str(x.get('situacao_pedido') or '').strip()), '—')))

        if cancelado_721:
            continue
        if billed <= 0.01:
            invoice_status='NÃO FATURADO'
        elif remaining > 0.01:
            invoice_status='PARCIAL'
        else:
            invoice_status='FATURADO'

        # Na visão normal mostramos o que o 721 ainda considera pendente e que
        # também possui saldo financeiro após conciliação com o 731.
        if not include_faturados:
            if faturado_721 and not pendente_721:
                continue
            if remaining <= 0.01:
                continue

        projections=[_prever_entrega_721(x) for x in items]
        exact_projs=[p for p in projections if p.get('source')=='ENTREGA_PEDIDO' and p.get('date')]
        if exact_projs:
            delivery=max(exact_projs, key=lambda p: p.get('date'))
        else:
            dated=[p for p in projections if p.get('date')]
            delivery=(dated[0] if dated else projections[0]) if projections else _prever_entrega_721(base)
        entrega_iso=str(delivery.get('iso') or '')
        entrega=str(delivery.get('display') or '—')
        entrega_d=delivery.get('date')
        entrega_origem=str(delivery.get('label') or '')
        estoque_imediato=bool(delivery.get('stock'))
        if entrega and entrega != '—': delivery_known += 1
        if remaining > 0.01 and entrega_d:
            if entrega_d < today:
                delivery_status='ATRASADO'
            elif entrega_d == today:
                delivery_status='ENTREGA HOJE'
            else:
                delivery_status='A VENCER'
            if estoque_imediato:
                delivery_status += ' · JIT/ESTOQUE'
        elif remaining > 0.01 and estoque_imediato:
            delivery_status='JIT IMEDIATO · ESTOQUE'
        elif remaining > 0.01:
            delivery_status='PREVISÃO POR CONDIÇÃO' if entrega and entrega != '—' else 'SEM PREVISÃO'
        else:
            delivery_status='CONCLUÍDO'

        # Na grade principal usamos o grupo/produto resumido do 721 (coluna O).
        # Código V e descrição completa W ficam no detalhe do item, evitando uma
        # coluna principal enorme e mantendo toda a informação auditável.
        prod_labels=[]
        for x in items:
            grp=str(x.get('grupo') or '').strip(); desc=str(x.get('produto') or '').strip()
            label=grp or desc
            if label and label not in prod_labels: prod_labels.append(label)
        if not prod_labels:
            for x in invs:
                lab=str(x.get('produto') or '').strip()
                if lab and lab not in prod_labels: prod_labels.append(lab)
        if prod_labels: product_known += 1
        measures=[]; papers=[]; groups=[]; product_codes=[]; product_descs=[]
        for x in items:
            for arr,key in ((measures,'medida'),(papers,'papel'),(groups,'grupo'),(product_codes,'produto_codigo'),(product_descs,'produto')):
                val=str(x.get(key) or '').strip()
                if val and val not in arr: arr.append(val)
        if not measures:
            for x in invs:
                val=str(x.get('medida') or '').strip()
                if val and val not in measures: measures.append(val)
        nfs=[]
        for x in invs:
            nf=str(x.get('nf') or '').strip()
            if nf and nf not in nfs: nfs.append(nf)
        inv_dates=sorted({str(x.get('data_emissao') or '') for x in invs if x.get('data_emissao')})
        pay_dates=sorted({str(x.get('data_pagto') or '') for x in liqs if x.get('data_pagto')})

        item_detail=[]
        for x in items:
            code=str(x.get('produto_codigo') or '').strip(); desc=str(x.get('produto') or '').strip()
            item_detail.append({
                'produto': str(x.get('grupo') or '').strip() or desc or '—',
                'codigo': code,
                'descricao': desc,
                'medida':x.get('medida') or '', 'papel':x.get('papel') or '',
                'quantidade':float(x.get('quantidade') or 0),
                'preco_unitario':float(x.get('preco_unitario') or 0),
                'valor_item':float(x.get('valor_item') or 0),
                'observacao':x.get('observacao') or '',
            })
        row={
            'pedido':pedido_display,'data_pedido':base.get('data_emissao') or '', 'entrega':entrega, 'entrega_iso':entrega_iso,
            'entrega_origem':entrega_origem, 'estoque_imediato':estoque_imediato,
            'cliente':base.get('cliente') or '—','vendedor':base.get('vendedor') or '—','time':base.get('time') or 'EXTERNO',
            'produtos':' | '.join(prod_labels) if prod_labels else '—','medidas':' | '.join(measures) if measures else '—',
            'papeis':' | '.join(papers) if papers else '—','grupos':' | '.join(groups) if groups else '—',
            'codigos_produto':' | '.join(product_codes) if product_codes else '—',
            'descricoes_produto':' | '.join(product_descs) if product_descs else '—',
            'quantidade':ordered_qty,'valor_pedido':ordered_value,'faturado':billed,'qtd_faturada':billed_qty,
            'falta_faturar':remaining,'pct_faturado':(billed/ordered_value*100.0 if ordered_value>0 else 0.0),
            'liquidado':paid,'a_receber':receivable,'status':invoice_status,'situacao_721':situacao_721,'entrega_status':delivery_status,
            'nfs':', '.join(nfs),'ultima_nf':inv_dates[-1] if inv_dates else '', 'ultimo_pagamento':pay_dates[-1] if pay_dates else '',
            'condicao_entrega':base.get('condicao_entrega') or '', 'observacao':base.get('observacao') or '', 'items':item_detail,
        }
        if vendedor_n and vendedor_n not in _legacy().norm(row['vendedor']): continue
        if cliente_n and cliente_n not in _legacy().norm(row['cliente']): continue
        if produto_n and produto_n not in _legacy().norm(row['produtos']+' '+row['grupos']+' '+row['papeis']+' '+row.get('codigos_produto','')+' '+row.get('descricoes_produto','')): continue
        if medida_n and medida_n not in _legacy().norm(row['medidas']): continue
        if status_n and status_n not in {'TODOS','TOTAL'}:
            if status_n not in {row['status'],row['entrega_status']} and status_n not in _legacy().norm(row['status']+' '+row['entrega_status']).upper(): continue
        if entrega_ini and (not entrega_iso or entrega_iso < entrega_ini): continue
        if entrega_fim and (not entrega_iso or entrega_iso > entrega_fim): continue
        if year is not None and months:
            # Filtro mensal padrão da tela A Faturar é pelo mês de emissão do 721.
            # Data prevista possui filtros próprios (Entrega de/até).
            ref_date=row['data_pedido']
            try: rd=datetime.strptime(ref_date[:10],'%Y-%m-%d').date()
            except Exception: continue
            if rd.year != int(year) or rd.month not in months: continue
        rows.append(row)

    rows.sort(key=lambda r: (0 if str(r['entrega_status']).startswith('ATRASADO') else 1, r.get('entrega_iso') or '9999-12-31', r['vendedor'], r['pedido']))
    by_vendor=defaultdict(lambda:{'pedidos':0,'falta':0.0,'faturado':0.0,'liquidado':0.0})
    for r in rows:
        a=by_vendor[(r['vendedor'],r['time'])]; a['pedidos']+=1; a['falta']+=r['falta_faturar']; a['faturado']+=r['faturado']; a['liquidado']+=r['liquidado']
    vendor_rows=[{'vendedor':k[0],'time':k[1],**v} for k,v in by_vendor.items()]
    vendor_rows.sort(key=lambda x:-x['falta'])
    return {
        'rows':rows,'vendors':vendor_rows,
        'total_pedidos':len(rows),'total_pedido':sum(r['valor_pedido'] for r in rows),'total_faturado':sum(r['faturado'] for r in rows),
        'total_falta':sum(r['falta_faturar'] for r in rows),'total_liquidado':sum(r['liquidado'] for r in rows),'total_a_receber':sum(r['a_receber'] for r in rows),
        'atrasados':sum(1 for r in rows if str(r['entrega_status']).startswith('ATRASADO')),
        'parciais':sum(1 for r in rows if r['status']=='PARCIAL'),
        'delivery_coverage':(sum(1 for r in rows if (r.get('entrega') or '') not in ('','—'))/len(rows)*100.0 if rows else 0.0),
        'product_coverage':(sum(1 for r in rows if (r.get('produtos') or '') not in ('','—'))/len(rows)*100.0 if rows else 0.0),
    }
