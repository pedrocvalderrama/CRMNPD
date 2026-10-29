"""Planejamento sazonal / forecast / consumo de papel (CRM 10.10)."""
from __future__ import annotations

import calendar
import re
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from crm_npd.services.relatorios import (
    MONTH_NAME_PT,
    MONTHS_PT,
    _analysis_scope_sql,
    _historical_period_selector,
    _legacy,
    _report_date_in_months,
    _report_datasets,
    _report_months_label,
    _report_selected_months,
    _resolve_report_period,
)


def _weighted_history(year_values: Dict[int, float], source_years: List[int]) -> float:
    """Média ponderada sazonal: 50% ano mais recente, 30% segundo, 20% terceiro."""
    if not source_years:
        return 0.0
    base_weights = [0.50, 0.30, 0.20]
    weights = base_weights[:len(source_years)]
    total_w = sum(weights) or 1.0
    return sum(float(year_values.get(y, 0.0) or 0.0) * w for y, w in zip(source_years, weights)) / total_w


def _production_family(grupo: str, produto: str) -> str:
    text = _legacy().norm(f"{grupo or ''} {produto or ''}")
    if "CARTUCHO" in text:
        return "Cartuchos"
    if "SACOLA" in text:
        return "Sacolas"
    if "CAIXA" in text or text.startswith("CX "):
        return "Caixas"
    if "PAPEL" in text:
        return "Papéis"
    if "OVERBAG" in text:
        return "Overbag"
    return "Outros"


def _production_grammage(papel: str, produto: str) -> str:
    text = f"{papel or ''} {produto or ''}".upper()
    m = re.search(r"(?<!\d)(\d{2,3})\s*(?:GRS?|G)(?![A-Z])", text)
    if m:
        return f"{int(m.group(1))} g"
    return "Sem gramatura cadastrada"


# -------------------------------------------------------------------------
# V4.8.0 — PLANEJAMENTO GERENCIAL / SAZONALIDADE / CONSUMO DE PAPEL
# -------------------------------------------------------------------------
def _period_ref_for_year(periodo: str, anchor: date, year: int) -> date:
    """Recria a mesma posição sazonal em outro ano."""
    p = (periodo or "mes").lower()
    try:
        if p == "semana":
            return datetime.strptime(f"{year} {int(anchor.strftime('%W')):02d} 1", "%Y %W %w").date()
        if p == "trimestre":
            return date(year, ((anchor.month - 1)//3)*3 + 1, 1)
        if p == "semestre":
            return date(year, 1 if anchor.month <= 6 else 7, 1)
        if p == "ano":
            return date(year, 1, 1)
        if p == "mes":
            return date(year, anchor.month, 1)
        import calendar
        return date(year, anchor.month, min(anchor.day, calendar.monthrange(year, anchor.month)[1]))
    except Exception:
        return date(year, 1, 1)


def _period_months(periodo: str, anchor: date, year: int) -> List[int]:
    ref = _period_ref_for_year(periodo, anchor, year)
    d0, d1, _ = _resolve_report_period(periodo, ref)
    months = []
    cursor = date(d0.year, d0.month, 1)
    limit = date(d1.year, d1.month, 1)
    while cursor <= limit:
        if cursor.month not in months:
            months.append(cursor.month)
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


def _parse_grammage_gsm(papel: str, produto: str = "") -> Optional[int]:
    text = f"{papel or ''} {produto or ''}".upper()
    hits = re.findall(r"(?<!\d)(\d{2,3})\s*(?:GRS?|G)(?![A-Z])", text)
    if not hits:
        return None
    try:
        gsm = int(hits[-1])
        return gsm if 20 <= gsm <= 400 else None
    except Exception:
        return None


def _parse_measure_triplet(*texts: str) -> Optional[Tuple[float, float, float]]:
    """Extrai L x A x F em cm. Priorize descrição do produto antes do formato cadastrado."""
    pat = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*[xX×]\s*(\d{1,3}(?:[.,]\d+)?)\s*[xX×]\s*(\d{1,3}(?:[.,]\d+)?)")
    for txt in texts:
        if not txt:
            continue
        hits = pat.findall(str(txt))
        if hits:
            a, b, c = hits[-1]
            try:
                dims = tuple(float(x.replace(',', '.')) for x in (a,b,c))
                if all(1 <= x <= 150 for x in dims):
                    return dims  # largura, altura, fundo/lateral
            except Exception:
                pass
    return None


def _estimate_paper_kg(quantidade: float, papel: str, produto: str, formato: str, medida: str, grupo: str) -> Optional[Dict[str, Any]]:
    """Estimativa geométrica de papel para sacolas/cartuchos.

    Modelo de tubo: largura de bobina ~= 2*(L+F), corte ~= A+F.
    A perda técnica NÃO entra aqui; ela é aplicada separadamente no forecast.
    """
    qty = float(quantidade or 0)
    if qty <= 0:
        return None
    family = _production_family(grupo, produto)
    if family not in {"Sacolas", "Cartuchos", "Overbag"}:
        return None
    gsm = _parse_grammage_gsm(papel, produto)
    dims = _parse_measure_triplet(produto, formato, medida)
    if not gsm or not dims:
        return None
    largura, altura, fundo = dims
    web_cm = 2.0 * (largura + fundo)
    cut_cm = altura + fundo
    area_m2_unit = (web_cm * cut_cm) / 10000.0
    kg_unit = area_m2_unit * float(gsm) / 1000.0
    kg = kg_unit * qty
    if kg <= 0 or kg > 1e8:
        return None
    return {
        "kg": kg,
        "gsm": gsm,
        "medida": f"{largura:g}x{altura:g}x{fundo:g}",
        "kg_mil": kg_unit * 1000.0,
        "family": family,
    }


def _frequency_label(avg_days: Optional[float]) -> str:
    if not avg_days or avg_days <= 0:
        return "Sem padrão suficiente"
    if avg_days <= 40: return "Mensal"
    if avg_days <= 75: return "A cada ~2 meses"
    if avg_days <= 115: return "Trimestral"
    if avg_days <= 210: return "Semestral"
    if avg_days <= 430: return "Sazonal / anual"
    return "Esporádico"


def _avg_purchase_gap(dates: List[date]) -> Optional[float]:
    uniq = sorted(set(dates))
    if len(uniq) < 2:
        return None
    # últimos intervalos têm mais relevância e evitam distorção de histórico muito antigo
    gaps = [(b-a).days for a,b in zip(uniq[-10:-1], uniq[-9:])] if len(uniq) > 9 else [(b-a).days for a,b in zip(uniq, uniq[1:])]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return None
    return float(statistics.median(gaps))


def _auto_growth(year_stats: Dict[int, Dict[str, Any]], key: str, target_year: int) -> Tuple[float, float, float, float]:
    """Tendência automática da janela sazonal para valor ou quantidade."""
    growth_samples = []
    ordered_prior = sorted([y for y in year_stats if y < target_year])
    for y0, y1 in zip(ordered_prior[:-1], ordered_prior[1:]):
        v0 = float(year_stats.get(y0, {}).get(key, 0) or 0)
        v1 = float(year_stats.get(y1, {}).get(key, 0) or 0)
        if v0 > 0 and v1 >= 0:
            growth_samples.append(max(-0.40, min(0.50, (v1 / v0) - 1.0)))
    recent_growth = growth_samples[-3:]
    if recent_growth:
        gw = [0.15, 0.30, 0.55][-len(recent_growth):]
        swg = sum(gw) or 1.0
        probable_growth = sum(g * w for g, w in zip(recent_growth, gw)) / swg
        dispersion = statistics.pstdev(recent_growth) if len(recent_growth) > 1 else abs(recent_growth[0]) * 0.35
        spread = max(0.05, min(0.20, dispersion if dispersion > 0 else 0.05))
    else:
        probable_growth = 0.0
        spread = 0.08
    probable_growth = max(-0.30, min(0.35, probable_growth))
    conservative_growth = max(-0.40, probable_growth - spread)
    optimistic_growth = min(0.50, probable_growth + spread)
    return probable_growth, conservative_growth, optimistic_growth, spread


def _planning_analysis_data(conn, periodo: str, anchor: date, target_year: int, scope: str,
                            growth: Optional[float] = None, scenario_band: Optional[float] = None,
                            waste_pct: Optional[float] = None, selected_months: Optional[List[int]] = None,
                            conservative_growth_pct: Optional[float] = None,
                            probable_growth_pct: Optional[float] = None,
                            optimistic_growth_pct: Optional[float] = None,
                            vendor_id: Optional[int] = None,
                            metric: Optional[str] = None,
                            fonte: Optional[str] = None) -> Dict[str, Any]:
    """Fonte única para tela e Excel de planejamento.

    Trimestre/semestre tratam a janela inteira como sazonalidade: um cliente que comprou
    em outubro num ano e novembro no seguinte continua sendo reconhecido como recorrente
    no mesmo trimestre. A provável distribuição mensal é calculada dentro da janela.
    """
    periodo = (periodo or "mes").lower()
    scope = (scope or "TOTAL").upper()
    metric = (metric or "valor").strip().lower()
    if metric == "faturamento":
        metric = "valor"
    if metric not in {"valor", "quantidade"}:
        metric = "valor"
    fonte = (fonte or "pedidos").strip().lower()
    if fonte not in {"pedidos", "faturamento"}:
        fonte = "pedidos"
    # V5.0: crescimento e amplitude dos cenários são automáticos, calculados pelo histórico
    # da mesma janela sazonal. O usuário não precisa mais escolher percentuais arbitrários.
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='paper_waste_pct'").fetchone()
        system_waste = float(str(row[0]).replace(',', '.')) if row and row[0] not in (None, '') else 8.0
    except Exception:
        system_waste = 8.0
    waste_pct = max(0.0, min(30.0, float(system_waste)))
    waste_factor = 1.0 + waste_pct/100.0
    scope_sql = _analysis_scope_sql(scope, "v")
    try:
        vendor_id = int(vendor_id) if vendor_id not in (None, "", 0, "0") else None
    except Exception:
        vendor_id = None
    vendor_sql = "v.id=?" if vendor_id else "1=1"
    vendor_params: Tuple[Any, ...] = (vendor_id,) if vendor_id else ()
    vendor_name = None
    if vendor_id:
        vrow = conn.execute("SELECT nome FROM vendors WHERE id=?", (vendor_id,)).fetchone()
        vendor_name = (vrow[0] if vrow else None) or None
    selected_months = sorted({int(m) for m in (selected_months or []) if 1 <= int(m) <= 12})
    if selected_months:
        qmarks = ",".join("?" for _ in selected_months)
        ord_cond = f"CAST(strftime('%m',o.data_emissao) AS INTEGER) IN ({qmarks})"
        inv_cond = f"CAST(strftime('%m',i.data_emissao) AS INTEGER) IN ({qmarks})"
        ord_params = tuple(selected_months)
        inv_params = tuple(selected_months)
        period_name = " + ".join(MONTH_NAME_PT.get(m, str(m)) for m in selected_months)
        periodo = "meses"
    else:
        ord_cond, ord_params, period_name = _historical_period_selector(periodo, anchor, "o")
        inv_cond, inv_params, _ = _historical_period_selector(periodo, anchor, "i")

    orders = [dict(r) for r in conn.execute(f"""
        SELECT CAST(strftime('%Y',o.data_emissao) AS INTEGER) ano,
               CAST(strftime('%m',o.data_emissao) AS INTEGER) mes,
               date(o.data_emissao) data, TRIM(COALESCE(o.pedido,'')) pedido,
               o.cliente_id, o.vendor_id,
               COALESCE(o.quantidade,0) quantidade,
               COALESCE(NULLIF(o.valor_item_ref,0),o.valor,0) valor,
               COALESCE(NULLIF(TRIM(c.rede_key),''), CASE WHEN c.id IS NOT NULL THEN 'CID:'||c.id ELSE 'NOME:'||UPPER(TRIM(COALESCE(o.cliente_nome_ref,''))) END) cliente_key,
               COALESCE(NULLIF(TRIM(c.rede_nome),''),NULLIF(TRIM(c.fantasia),''),NULLIF(TRIM(c.nome),''),NULLIF(TRIM(o.cliente_nome_ref),''),'—') cliente_nome,
               COALESCE(NULLIF(TRIM(v.nome),''),'—') vendedor,
               CASE WHEN COALESCE(UPPER(TRIM(v.nome)),'')='SITE' OR COALESCE(UPPER(TRIM(v.tipo)),'')='SITE' THEN 'SITE' ELSE COALESCE(UPPER(TRIM(v.tipo)),'EXTERNO') END time,
               COALESCE(o.grupo_ref,'') grupo, COALESCE(o.medida_ref,'') medida, COALESCE(o.papel_ref,'') papel
        FROM orders o LEFT JOIN customers c ON c.id=o.cliente_id LEFT JOIN vendors v ON v.id=o.vendor_id
        WHERE {ord_cond} AND {scope_sql} AND {vendor_sql} AND {_legacy().sql_pedidos_realizados_excel_cond('o','v')}
        ORDER BY ano,data,pedido
    """, ord_params + vendor_params).fetchall()]

    invoices = [dict(r) for r in conn.execute(f"""
        SELECT CAST(strftime('%Y',i.data_emissao) AS INTEGER) ano,
               CAST(strftime('%m',i.data_emissao) AS INTEGER) mes,
               date(i.data_emissao) data, COALESCE(i.numero,'') nf, COALESCE(i.pedido,'') pedido,
               i.cliente_id, i.vendor_id,
               COALESCE(i.grupo_ref,'') grupo, COALESCE(i.papel_ref,'') papel,
               COALESCE(i.produto,'') produto, COALESCE(i.formato,'') formato,
               COALESCE(i.medida_ref,'') medida, COALESCE(i.quantidade,0) quantidade,
               COALESCE(i.valor,0) valor,
               COALESCE(NULLIF(TRIM(c.rede_key),''), CASE WHEN c.id IS NOT NULL THEN 'CID:'||c.id ELSE 'NOME:'||UPPER(TRIM(COALESCE(i.cliente_nome_ref,''))) END) cliente_key,
               COALESCE(NULLIF(TRIM(c.rede_nome),''),NULLIF(TRIM(c.fantasia),''),NULLIF(TRIM(c.nome),''),NULLIF(TRIM(i.cliente_nome_ref),''),'—') cliente_nome,
               COALESCE(NULLIF(TRIM(c.fantasia),''),NULLIF(TRIM(c.nome),''),NULLIF(TRIM(i.cliente_nome_ref),''),'—') cliente,
               COALESCE(NULLIF(TRIM(v.nome),''),'—') vendedor,
               CASE WHEN COALESCE(UPPER(TRIM(v.nome)),'')='SITE' OR COALESCE(UPPER(TRIM(v.tipo)),'')='SITE' THEN 'SITE' ELSE COALESCE(UPPER(TRIM(v.tipo)),'EXTERNO') END time
        FROM invoices i LEFT JOIN customers c ON c.id=i.cliente_id LEFT JOIN vendors v ON v.id=i.vendor_id
        WHERE i.data_emissao IS NOT NULL AND {inv_cond} AND {scope_sql} AND {vendor_sql}
        ORDER BY ano,data,nf
    """, inv_params + vendor_params).fetchall()]

    sales_rows = invoices if fonte == "faturamento" else orders

    # Histórico completo para frequência / próxima recompra.
    if fonte == "faturamento":
        freq_rows = [dict(r) for r in conn.execute(f"""
            SELECT date(i.data_emissao) data, i.cliente_id, i.vendor_id,
                   COALESCE(NULLIF(TRIM(c.rede_key),''), CASE WHEN c.id IS NOT NULL THEN 'CID:'||c.id ELSE 'NOME:'||UPPER(TRIM(COALESCE(i.cliente_nome_ref,''))) END) cliente_key,
                   COALESCE(NULLIF(TRIM(c.rede_nome),''),NULLIF(TRIM(c.fantasia),''),NULLIF(TRIM(c.nome),''),NULLIF(TRIM(i.cliente_nome_ref),''),'—') cliente_nome
            FROM invoices i LEFT JOIN customers c ON c.id=i.cliente_id LEFT JOIN vendors v ON v.id=i.vendor_id
            WHERE i.data_emissao IS NOT NULL AND {scope_sql} AND {vendor_sql}
            ORDER BY date(i.data_emissao)
        """, vendor_params).fetchall()]
        db_years = [int(r[0]) for r in conn.execute("SELECT DISTINCT CAST(strftime('%Y',data_emissao) AS INTEGER) y FROM invoices WHERE data_emissao IS NOT NULL ORDER BY y").fetchall() if r[0]]
    else:
        freq_rows = [dict(r) for r in conn.execute(f"""
            SELECT date(o.data_emissao) data, o.cliente_id, o.vendor_id,
                   COALESCE(NULLIF(TRIM(c.rede_key),''), CASE WHEN c.id IS NOT NULL THEN 'CID:'||c.id ELSE 'NOME:'||UPPER(TRIM(COALESCE(o.cliente_nome_ref,''))) END) cliente_key,
                   COALESCE(NULLIF(TRIM(c.rede_nome),''),NULLIF(TRIM(c.fantasia),''),NULLIF(TRIM(c.nome),''),NULLIF(TRIM(o.cliente_nome_ref),''),'—') cliente_nome
            FROM orders o LEFT JOIN customers c ON c.id=o.cliente_id LEFT JOIN vendors v ON v.id=o.vendor_id
            WHERE o.data_emissao IS NOT NULL AND {scope_sql} AND {vendor_sql} AND {_legacy().sql_pedidos_realizados_excel_cond('o','v')}
            ORDER BY date(o.data_emissao)
        """, vendor_params).fetchall()]
        db_years = [int(r[0]) for r in conn.execute("SELECT DISTINCT CAST(strftime('%Y',data_emissao) AS INTEGER) y FROM orders WHERE data_emissao IS NOT NULL ORDER BY y").fetchall() if r[0]]

    year_stats: Dict[int, Dict[str, Any]] = {}
    clients_map: Dict[str, Dict[str, Any]] = {}
    for r in sales_rows:
        y=int(r.get('ano') or 0)
        if y <= 0: continue
        st=year_stats.setdefault(y,{"value":0.0,"qty":0.0,"orders":set(),"clients":set(),"months":defaultdict(float),"months_qty":defaultdict(float)})
        st['value'] += float(r.get('valor') or 0); st['qty'] += float(r.get('quantidade') or 0)
        if str(r.get('pedido') or '').strip(): st['orders'].add(str(r.get('pedido')).strip())
        ckey=str(r.get('cliente_key') or '').strip() or f"NOME:{_legacy().norm(r.get('cliente_nome') or r.get('cliente') or '—')}"
        mes_n=int(r.get('mes') or 0)
        st['clients'].add(ckey); st['months'][mes_n] += float(r.get('valor') or 0)
        st['months_qty'][mes_n] += float(r.get('quantidade') or 0)
        c=clients_map.setdefault(ckey,{"name":r.get('cliente_nome') or r.get('cliente') or '—',"years":{},"cliente_id":r.get('cliente_id'),"vendor_id":r.get('vendor_id')})
        if r.get('cliente_id'): c['cliente_id']=r.get('cliente_id')
        if r.get('vendor_id'): c['vendor_id']=r.get('vendor_id')
        cy=c['years'].setdefault(y,{"value":0.0,"qty":0.0,"orders":set(),"months":defaultdict(float)})
        cy['value'] += float(r.get('valor') or 0); cy['qty'] += float(r.get('quantidade') or 0); cy['months'][int(r.get('mes') or 0)] += float(r.get('valor') or 0)
        if str(r.get('pedido') or '').strip(): cy['orders'].add(str(r.get('pedido')).strip())

    available_prior=sorted([y for y in year_stats if y < target_year], reverse=True)
    source_years=available_prior[:3]
    base_weights=[0.50,0.30,0.20][:len(source_years)]
    sw=sum(base_weights) or 1.0
    weights={y:w/sw for y,w in zip(source_years,base_weights)}
    wavg=lambda getter: sum(float(getter(y) or 0)*weights.get(y,0) for y in source_years)

    baseline_value=wavg(lambda y: year_stats.get(y,{}).get('value',0))
    baseline_qty=wavg(lambda y: year_stats.get(y,{}).get('qty',0))
    baseline_orders=wavg(lambda y: len(year_stats.get(y,{}).get('orders',set())))
    baseline_clients=wavg(lambda y: len(year_stats.get(y,{}).get('clients',set())))

    # Tendência automática do mesmo período entre anos. Evita um único salto extremo
    # dominar a projeção e usa maior peso no crescimento mais recente.
    probable_growth, conservative_growth, optimistic_growth, spread = _auto_growth(year_stats, "value", target_year)
    qty_probable, qty_cons, qty_opt, qty_spread = _auto_growth(year_stats, "qty", target_year)
    if probable_growth_pct is not None:
        probable_growth = float(probable_growth_pct) / 100.0
        qty_probable = probable_growth
    if conservative_growth_pct is not None:
        conservative_growth = float(conservative_growth_pct) / 100.0
        qty_cons = conservative_growth
    if optimistic_growth_pct is not None:
        optimistic_growth = float(optimistic_growth_pct) / 100.0
        qty_opt = optimistic_growth
    growth=probable_growth*100.0
    scenario_band=spread*100.0
    growth_factor=1.0+probable_growth
    qty_growth_factor=1.0+qty_probable

    forecast_value=baseline_value*growth_factor
    forecast_qty=baseline_qty*qty_growth_factor
    forecast_orders=baseline_orders*growth_factor
    forecast_clients=baseline_clients*growth_factor
    scenarios={
        'conservative': baseline_value*(1.0+conservative_growth),
        'probable': forecast_value,
        'optimistic': baseline_value*(1.0+optimistic_growth),
        'conservative_growth': conservative_growth*100.0,
        'probable_growth': probable_growth*100.0,
        'optimistic_growth': optimistic_growth*100.0,
        'spread': spread*100.0,
    }
    scenarios_qty={
        'conservative': baseline_qty*(1.0+qty_cons),
        'probable': forecast_qty,
        'optimistic': baseline_qty*(1.0+qty_opt),
        'conservative_growth': qty_cons*100.0,
        'probable_growth': qty_probable*100.0,
        'optimistic_growth': qty_opt*100.0,
        'spread': qty_spread*100.0,
    }

    # Frequência de compra por identidade comercial.
    freq_map=defaultdict(lambda:{"dates":[],"cliente_id":None,"vendor_id":None,"name":"—"})
    for r in freq_rows:
        try: d=datetime.strptime(str(r.get('data')),'%Y-%m-%d').date()
        except Exception: continue
        key=str(r.get('cliente_key') or '').strip() or f"NOME:{_legacy().norm(r.get('cliente_nome') or '—')}"
        f=freq_map[key]; f['dates'].append(d); f['name']=r.get('cliente_nome') or f['name']
        if r.get('cliente_id'): f['cliente_id']=r.get('cliente_id')
        if r.get('vendor_id'): f['vendor_id']=r.get('vendor_id')

    recurrence_years=sorted([y for y in year_stats if y < target_year], reverse=True)[:5]
    latest_prior=recurrence_years[0] if recurrence_years else None
    if selected_months:
        import calendar
        target_months=list(selected_months)
        target_ref=date(target_year,target_months[0],1)
        target_d0=date(target_year,target_months[0],1)
        target_d1=date(target_year,target_months[-1],calendar.monthrange(target_year,target_months[-1])[1])
    else:
        target_ref=_period_ref_for_year(periodo,anchor,target_year)
        target_d0,target_d1,_=_resolve_report_period(periodo,target_ref)
        target_months=_period_months(periodo,anchor,target_year)
    expected=[]
    for key,c in clients_map.items():
        bought_years=[y for y in recurrence_years if float(c['years'].get(y,{}).get('value',0) or 0)>0]
        if not bought_years: continue
        if latest_prior not in bought_years and len(bought_years)<2: continue
        projected=sum(float(c['years'].get(y,{}).get('value',0) or 0)*weights.get(y,0) for y in source_years)*growth_factor
        target_value=float(c['years'].get(target_year,{}).get('value',0) or 0)
        month_project={m: sum(float(c['years'].get(y,{}).get('months',{}).get(m,0) or 0)*weights.get(y,0) for y in source_years)*growth_factor for m in target_months}
        likely_month=max(month_project, key=month_project.get) if month_project and max(month_project.values(),default=0)>0 else (target_months[0] if target_months else anchor.month)
        gap=max(projected-target_value,0.0)
        f=freq_map.get(key,{})
        avg_gap=_avg_purchase_gap(f.get('dates',[]))
        last_purchase=max(f.get('dates',[])) if f.get('dates') else None
        next_purchase=(last_purchase+timedelta(days=int(round(avg_gap)))) if last_purchase and avg_gap else None
        if target_value>0:
            status='Já comprou' if target_value >= projected*0.75 else 'Comprou abaixo'
        elif target_year > date.today().year or target_d0 > date.today():
            status='Previsto'
        elif target_year == date.today().year and likely_month > date.today().month and date.today() <= target_d1:
            status='Janela futura'
        else:
            status='Oportunidade'
        expected.append({
            'key':key,'name':c['name'],'years':bought_years,'recurrence':len(bought_years),'projected':projected,
            'target_value':target_value,'gap':gap,'status':status,'likely_month':likely_month,
            'likely_month_name':MONTH_NAME_PT.get(likely_month,str(likely_month)),
            'month_project':month_project,'avg_gap':avg_gap,'frequency':_frequency_label(avg_gap),
            'last_purchase':last_purchase,'next_purchase':next_purchase,
            'cliente_id':c.get('cliente_id') or f.get('cliente_id'),'vendor_id':c.get('vendor_id') or f.get('vendor_id')
        })
    expected.sort(key=lambda x:(x['gap'],x['projected'],x['recurrence']),reverse=True)
    revenue_risk=sum(x['gap'] for x in expected if x['status']!='Já comprou')
    pending_clients=sum(1 for x in expected if x['status'] in {'Oportunidade','Comprou abaixo'})

    # Evolução mensal interna da janela (essencial para trimestre/semestre).
    month_forecast=[]
    month_forecast_qty=[]
    for m in target_months:
        base=sum(float(year_stats.get(y,{}).get('months',{}).get(m,0) or 0)*weights.get(y,0) for y in source_years)
        actual=float(year_stats.get(target_year,{}).get('months',{}).get(m,0) or 0)
        month_forecast.append({'month':m,'name':MONTH_NAME_PT.get(m,str(m)),'baseline':base,'forecast':base*growth_factor,'actual':actual})
        base_q=sum(float(year_stats.get(y,{}).get('months_qty',{}).get(m,0) or 0)*weights.get(y,0) for y in source_years)
        actual_q=float(year_stats.get(target_year,{}).get('months_qty',{}).get(m,0) or 0)
        month_forecast_qty.append({'month':m,'name':MONTH_NAME_PT.get(m,str(m)),'baseline':base_q,'forecast':base_q*qty_growth_factor,'actual':actual_q})

    # Produção e consumo físico de papel.
    prod_year_total=defaultdict(float); family_by_year=defaultdict(lambda:defaultdict(float)); gram_qty_by_year=defaultdict(lambda:defaultdict(float))
    measure_qty_by_year=defaultdict(lambda:defaultdict(float)); measure_kg_by_year=defaultdict(lambda:defaultdict(float))
    kg_by_year=defaultdict(float); kg_gram_by_year=defaultdict(lambda:defaultdict(float)); kg_month_by_year=defaultdict(lambda:defaultdict(float)); kg_month_gram_by_year=defaultdict(lambda:defaultdict(lambda:defaultdict(float)))
    eligible_qty=0.0; estimated_qty=0.0
    for r in sales_rows:
        y=int(r.get('ano') or 0); m=int(r.get('mes') or 0); q=float(r.get('quantidade') or 0)
        if y<=0: continue
        fam=_production_family(r.get('grupo'),r.get('produto')); gram=_production_grammage(r.get('papel'),r.get('produto'))
        measure=str(r.get('medida') or r.get('formato') or '').strip() or 'Sem medida'
        prod_year_total[y]+=q; family_by_year[y][fam]+=q; gram_qty_by_year[y][gram]+=q; measure_qty_by_year[y][measure]+=q
        if y in source_years and fam in {'Sacolas','Cartuchos','Overbag'}: eligible_qty += max(q,0)
        est=_estimate_paper_kg(q,r.get('papel'),r.get('produto'),r.get('formato') or r.get('medida'),r.get('medida'),r.get('grupo'))
        if est:
            kg=float(est['kg']); glabel=f"{est['gsm']} g"
            kg_by_year[y]+=kg; kg_gram_by_year[y][glabel]+=kg; kg_month_by_year[y][m]+=kg; kg_month_gram_by_year[y][m][glabel]+=kg; measure_kg_by_year[y][measure]+=kg
            if y in source_years: estimated_qty += max(q,0)

    prod_base=sum(float(prod_year_total.get(y,0))*weights.get(y,0) for y in source_years)
    prod_forecast_qty=prod_base*growth_factor
    fams=sorted({k for y in source_years for k in family_by_year[y]})
    fam_proj=[]
    for fam in fams:
        qty=sum(float(family_by_year[y].get(fam,0))*weights.get(y,0) for y in source_years)*growth_factor
        fam_proj.append({'name':fam,'qty':qty})
    fam_proj.sort(key=lambda x:x['qty'],reverse=True); fam_total=sum(x['qty'] for x in fam_proj) or 1.0
    for x in fam_proj: x['pct']=x['qty']/fam_total*100.0

    measures=sorted({k for y in source_years for k in measure_qty_by_year[y]})
    measure_proj=[]
    for measure in measures:
        qty=sum(float(measure_qty_by_year[y].get(measure,0))*weights.get(y,0) for y in source_years)*growth_factor
        kg=sum(float(measure_kg_by_year[y].get(measure,0))*weights.get(y,0) for y in source_years)*growth_factor*waste_factor
        if qty>0:
            measure_proj.append({'name':measure,'qty':qty,'kg':kg})
    measure_proj.sort(key=lambda x:x['qty'],reverse=True)
    measure_total=sum(x['qty'] for x in measure_proj) or 1.0
    for x in measure_proj: x['pct']=x['qty']/measure_total*100.0
    top_measures=measure_proj[:5]

    grams=sorted({k for y in source_years for k in gram_qty_by_year[y]})
    gram_proj=[]
    for gram in grams:
        qty=sum(float(gram_qty_by_year[y].get(gram,0))*weights.get(y,0) for y in source_years)*growth_factor
        gram_proj.append({'name':gram,'qty':qty})
    gram_proj.sort(key=lambda x:x['qty'],reverse=True); gram_total=sum(x['qty'] for x in gram_proj) or 1.0
    for x in gram_proj: x['pct']=x['qty']/gram_total*100.0

    kg_grams=sorted({k for y in source_years for k in kg_gram_by_year[y]}, key=lambda z: int(re.sub(r'\D','',z) or 9999))
    paper_consumption=[]
    for gram in kg_grams:
        kg=sum(float(kg_gram_by_year[y].get(gram,0))*weights.get(y,0) for y in source_years)*growth_factor*waste_factor
        paper_consumption.append({'gram':gram,'kg':kg,'ton':kg/1000.0})
    paper_consumption.sort(key=lambda x:x['kg'],reverse=True); total_paper_kg=sum(x['kg'] for x in paper_consumption)
    for x in paper_consumption: x['pct']=(x['kg']/total_paper_kg*100.0) if total_paper_kg else 0.0

    monthly_consumption=[]
    for m in target_months:
        grams_row=[]; total=0.0
        for gram in kg_grams:
            kg=sum(float(kg_month_gram_by_year[y][m].get(gram,0))*weights.get(y,0) for y in source_years)*growth_factor*waste_factor
            if kg>0: grams_row.append({'gram':gram,'kg':kg})
            total+=kg
        monthly_consumption.append({'month':m,'name':MONTH_NAME_PT.get(m,str(m)),'kg':total,'ton':total/1000.0,'grams':grams_row,'gram_map':{x['gram']:x['kg'] for x in grams_row}})
    paper_coverage=(estimated_qty/eligible_qty*100.0) if eligible_qty>0 else 0.0

    # Realizado do ano alvo.
    target_st=year_stats.get(target_year,{"value":0.0,"qty":0.0,"orders":set(),"clients":set()})
    actual_target_value=float(target_st.get('value') or 0); actual_target_qty=float(target_st.get('qty') or 0)
    actual_vs_forecast=((actual_target_value/forecast_value-1.0)*100.0) if forecast_value>0 and actual_target_value>0 else None
    actual_prod_qty=float(prod_year_total.get(target_year,0) or 0)

    # Pipeline de orçamentos do período alvo, sem somar automaticamente ao forecast para evitar dupla contagem.
    pipeline_value=0.0; pipeline_count=0
    try:
        catalog=_legacy()._performance_vendor_catalog(conn); vendors,_byid,bycode=catalog
        orcs=[dict(r) for r in conn.execute("SELECT vendedor_id,vendedor_nome,valor_total,pedido_id,status,desc_status,data_inicio FROM orcamentos WHERE date(data_inicio) BETWEEN date(?) AND date(?)",(target_d0.isoformat(),target_d1.isoformat())).fetchall()]
        for o in orcs:
            if selected_months and not _report_date_in_months(o.get('data_inicio'), target_year, selected_months):
                continue
            _k,meta=_legacy()._performance_resolve_orc_vendor(vendors,bycode,o.get('vendedor_id'),o.get('vendedor_nome'))
            tm=str(meta.get('tipo') or '').upper()
            if scope in {'INTERNO','EXTERNO','SITE'} and tm!=scope: continue
            if vendor_id:
                try: mid=int(meta.get('id') or 0)
                except Exception: mid=0
                if mid!=vendor_id: continue
            if str(o.get('pedido_id') or '').strip() not in {'','0'}: continue
            desc=_legacy().norm(o.get('desc_status') or o.get('status') or '')
            if any(x in desc for x in ['CANCEL','PERD','REPROV']): continue
            pipeline_value += float(o.get('valor_total') or 0); pipeline_count += 1
    except Exception:
        pass

    year_rows=[]
    for y in sorted(year_stats,reverse=True):
        st=year_stats[y]; prev=year_stats.get(y-1); variation=None
        if prev and float(prev.get('value') or 0): variation=(float(st.get('value') or 0)/float(prev.get('value'))-1)*100.0
        year_rows.append({'year':y,'value':float(st.get('value') or 0),'qty':float(st.get('qty') or 0),'orders':len(st.get('orders',set())),'clients':len(st.get('clients',set())),'variation':variation,'ref':(date(y, selected_months[0], 1).isoformat() if selected_months else _period_ref_for_year(periodo,anchor,y).isoformat())})

    min_db_year=min(db_years) if db_years else target_year-5; max_db_year=max(db_years) if db_years else target_year
    return {
        'periodo':periodo,'period_name':period_name,'anchor':anchor,'target_year':target_year,'scope':scope,'selected_months':selected_months,
        'vendor_id':vendor_id,'vendor_name':vendor_name,
        'growth':growth,'scenario_band':scenario_band,'waste_pct':waste_pct,'source_years':source_years,
        'source_text':' / '.join(map(str,source_years)) if source_years else 'sem histórico anterior suficiente',
        'baseline_value':baseline_value,'forecast_value':forecast_value,'forecast_qty':forecast_qty,'forecast_orders':forecast_orders,'forecast_clients':forecast_clients,
        'metric':metric,'fonte':fonte,
        'scenarios':scenarios,'scenarios_qty':scenarios_qty,'year_rows':year_rows,'expected_clients':expected,'revenue_risk':revenue_risk,'pending_clients':pending_clients,
        'month_forecast':month_forecast,'month_forecast_qty':month_forecast_qty,'prod_forecast_qty':prod_forecast_qty,'fam_proj':fam_proj,'gram_proj':gram_proj,'top_measures':top_measures,
        'paper_consumption':paper_consumption,'monthly_consumption':monthly_consumption,'total_paper_kg':total_paper_kg,'paper_coverage':paper_coverage,
        'actual_target_value':actual_target_value,'actual_target_qty':actual_target_qty,'actual_vs_forecast':actual_vs_forecast,'actual_prod_qty':actual_prod_qty,
        'pipeline_value':pipeline_value,'pipeline_count':pipeline_count,'target_d0':target_d0,'target_d1':target_d1,
        'min_db_year':min_db_year,'max_db_year':max_db_year,'orders':orders,'invoices':invoices,
    }

