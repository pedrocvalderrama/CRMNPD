"""Ciclo de vida de vendedores (ativos / inativos) portado do CRM 10.10."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

from crm_npd.services.relatorios import _legacy


def _vendor_team_alias_key(nome):
    """Normaliza aliases óbvios do mesmo vendedor para classificação de equipe.

    O Sisplan possui alguns cadastros históricos com ponto inicial ou CPF
    anexado ao nome. Esses registros representam a mesma pessoa e não podem
    ficar em equipes diferentes.
    """
    txt = str(nome or "").strip().lstrip(".").strip()
    txt = re.sub(r"\s+\d{11}$", "", txt).strip()
    return _legacy()._norm_jit_text_py(txt)




def _subtract_months(ref: date, months: int) -> date:
    total = ref.year * 12 + (ref.month - 1) - int(months)
    year, month0 = divmod(total, 12)
    month = month0 + 1
    last = (date(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(ref.day, last))


def _vendor_alias_ids(conn, vendor_id: int):
    """IDs inequívocos da mesma pessoa (ponto inicial / CPF ao final)."""
    row = conn.execute("SELECT id,nome FROM vendors WHERE id=?", (int(vendor_id),)).fetchone()
    if not row:
        return []
    key = _vendor_team_alias_key(row["nome"])
    if not key or key == "SITE":
        return [int(vendor_id)]
    ids = []
    for r in conn.execute("SELECT id,nome FROM vendors").fetchall():
        if _vendor_team_alias_key(r["nome"]) == key:
            ids.append(int(r["id"]))
    return sorted(set(ids or [int(vendor_id)]))


def _vendor_cache_clear():
    try:
        from .services.performance import clear_performance_caches
        clear_performance_caches()
    except Exception:
        pass
    try:
        from .services.score import clear_score_cache
        clear_score_cache()
    except Exception:
        pass


def _sync_vendedores_inativos(conn, hoje: Optional[date] = None) -> int:
    """Move automaticamente para Inativos quem está 3 meses sem venda.

    Só avalia vendedores atualmente ativos dos times Interno/Externo. SITE fica
    fora da regra. Vendedor reativado manualmente ganha uma nova janela de 3
    meses, mesmo que ainda não tenha realizado a primeira venda após o retorno.
    Um inativo nunca é reativado automaticamente: a recuperação é uma decisão
    explícita do gestor.
    """
    _legacy().ensure_vendors_schema(conn)
    hoje = hoje or date.today()
    cutoff = _subtract_months(hoje, 3)
    vendors = [dict(r) for r in conn.execute("""
        SELECT id,nome,codigo,tipo,ativo,created_at,reativado_em
        FROM vendors
        WHERE COALESCE(ativo,1)=1
          AND COALESCE(UPPER(TRIM(tipo)),'EXTERNO') IN ('INTERNO','EXTERNO')
          AND COALESCE(UPPER(TRIM(nome)),'') <> 'SITE'
    """).fetchall()]
    if not vendors:
        return 0

    last_sale = {int(r["vendor_id"]): r["ultima"] for r in conn.execute(f"""
        SELECT o.vendor_id, MAX(date(o.data_emissao)) AS ultima
        FROM orders o
        LEFT JOIN vendors v ON v.id=o.vendor_id
        WHERE o.vendor_id IS NOT NULL
          AND o.data_emissao IS NOT NULL
          AND {_legacy().sql_pedidos_realizados_excel_cond('o','v')}
        GROUP BY o.vendor_id
    """).fetchall() if r["vendor_id"] is not None}

    groups = {}
    for v in vendors:
        key = _vendor_team_alias_key(v.get("nome")) or f"ID:{v['id']}"
        groups.setdefault(key, []).append(v)

    changed = 0
    for _key, members in groups.items():
        ids = [int(v["id"]) for v in members]
        dates = []
        for v in members:
            raw = last_sale.get(int(v["id"]))
            if raw:
                try: dates.append(datetime.strptime(str(raw)[:10], "%Y-%m-%d").date())
                except Exception: pass
            raw2 = v.get("reativado_em")
            if raw2:
                try: dates.append(datetime.strptime(str(raw2)[:10], "%Y-%m-%d").date())
                except Exception: pass
        # created_at só serve de carência para um vendedor realmente novo,
        # isto é, sem nenhuma venda histórica. Importações antigas podem criar
        # o cadastro hoje, mas isso não transforma uma venda de 2022 em recente.
        if not dates:
            for v in members:
                raw3 = v.get("created_at")
                if raw3:
                    try: dates.append(datetime.strptime(str(raw3)[:10], "%Y-%m-%d").date())
                    except Exception: pass
        anchor = max(dates) if dates else None
        if anchor is not None and anchor >= cutoff:
            continue
        marks = ",".join("?" for _ in ids)
        cur = conn.execute(f"""UPDATE vendors
            SET ativo=0, performance_ativo=0,
                inativado_em=COALESCE(inativado_em, ?),
                inativado_motivo='SEM_VENDA_3_MESES'
            WHERE id IN ({marks}) AND COALESCE(ativo,1)=1""", [_legacy().now_iso(), *ids])
        changed += int(cur.rowcount or 0)
    if changed:
        conn.commit()
        _vendor_cache_clear()
    return changed
