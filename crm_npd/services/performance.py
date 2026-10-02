from __future__ import annotations

import json
import os
import hashlib
import copy
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime
from statistics import median
from typing import Any, Optional

from ..config import DB_PATH, CACHE_TTL_SECONDS
from ..metrics import recompra as f_recompra
from .cache import TTLCache

_customer_cache = TTLCache()
_score_cache = TTLCache()
_repurchase_cache = TTLCache()
_identity_name_cache = TTLCache()
_chart_cache = TTLCache()
_vendor_period_mix_cache = TTLCache()
_invoice_history_cache = TTLCache()
_first_invoice_doc_cache = TTLCache()
_jit_orders_cache = TTLCache()
_performance_context_cache = TTLCache()
_performance_annual_cache = TTLCache()


_GENERIC_CUSTOMER_NAMES = {
    "", "CLIENTE", "CLIENT", "CLIENTE FINAL", "CONSUMIDOR", "CONSUMIDOR FINAL",
    "NAO INFORMADO", "SEM NOME", "SEM CADASTRO", "N/A", "NA", "-", ".", "0",
    "CLIENTE NAO IDENTIFICADO",
}


def _name_key(value: Any) -> str:
    txt = str(value or "").strip().upper()
    txt = "".join(ch for ch in unicodedata.normalize("NFKD", txt) if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", txt).strip()


def _is_generic_name(value: Any) -> bool:
    key = _name_key(value)
    if key in _GENERIC_CUSTOMER_NAMES:
        return True
    if re.fullmatch(r"CLIENTE\s*#?\s*\d*", key):
        return True
    if re.fullmatch(r"CLIENTE\s+PEDIDO\s+\d+", key):
        return True
    return False


def _safe_display_name(nome: Any, fantasia: Any, codigo: Any, customer_id: Any) -> str:
    for value in (fantasia, nome):
        txt = str(value or "").strip()
        if txt and not _is_generic_name(txt):
            return txt
    code = str(codigo or "").strip()
    if code:
        return f"Cliente {code}"
    try:
        cid = int(customer_id or 0)
    except Exception:
        cid = 0
    return f"Cliente #{cid}" if cid else "Cliente não identificado"


def _db_version() -> str:
    try:
        st = os.stat(DB_PATH)
        return f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return "0"


def _user_sig(user: dict | None) -> tuple:
    if not user:
        return ("anon", None, None)
    return (
        (user.get("role") or "").strip().lower(),
        user.get("id"),
        user.get("vendor_codigo") or user.get("vendor_id"),
    )


def clear_performance_caches() -> None:
    _customer_cache.clear()
    _score_cache.clear()
    _repurchase_cache.clear()
    _identity_name_cache.clear()
    _chart_cache.clear()
    _vendor_period_mix_cache.clear()
    _invoice_history_cache.clear()
    _first_invoice_doc_cache.clear()
    _jit_orders_cache.clear()
    _performance_context_cache.clear()
    _performance_annual_cache.clear()


def list_customer_identities_fast(legacy, conn, user, q: str = "", limit: int = 300):
    """Versão em lote da carteira comercial.

    Evita o padrão N+1 da implementação legada (uma ou várias queries por cliente).
    A base inteira é resolvida em uma única consulta com os vínculos já persistidos.
    """
    version = _db_version()
    key = ("customer_identities", _user_sig(user), (q or "").strip().upper(), int(limit or 300))
    cached = _customer_cache.get(key, version=version)
    if cached is not None:
        return cached

    role = ((user.get("role") if user else "") or "").strip().lower()
    vendor_lock = legacy.get_user_vendor_id(conn, user) if user else None
    params: list[Any] = []
    vendor_sql = ""
    if role not in ("admin", "gestor") and vendor_lock:
        vendor_sql = """
            AND EXISTS (
                SELECT 1 FROM invoices iv
                WHERE iv.cliente_id = c.id AND iv.vendor_id = ?
            )
        """
        params.append(int(vendor_lock))

    rows = conn.execute(f"""
        SELECT
            c.id AS customer_id,
            COALESCE(c.codigo,'') AS codigo,
            COALESCE(c.nome,'') AS customer_nome_raw,
            COALESCE(c.fantasia,'') AS customer_fantasia_raw,
            COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || c.id) AS identity_key,
            COALESCE(cir.identity_nome,'') AS identity_nome_raw,
            UPPER(COALESCE(NULLIF(TRIM(cir.tipo_vinculo),''), 'CLIENTE')) AS identity_tipo,
            COALESCE(cir.matriz_customer_id,0) AS matriz_customer_id,
            COALESCE(NULLIF(TRIM(cm.fantasia),''), NULLIF(TRIM(cm.nome),''), '') AS matriz_nome
        FROM customers c
        LEFT JOIN customer_identity_rules cir ON cir.customer_id = c.id
        LEFT JOIN customers cm ON cm.id = cir.matriz_customer_id
        WHERE 1=1
        {vendor_sql}
        ORDER BY COALESCE(NULLIF(TRIM(cir.identity_nome),''), NULLIF(TRIM(c.fantasia),''), NULLIF(TRIM(c.nome),''), '') ASC
    """, params).fetchall()

    entidades: dict[str, dict[str, Any]] = {}
    prioridade = {"MATRIZ": 0, "REDE": 1, "FILIAL": 2, "CLIENTE": 3}
    for r in rows:
        ik = (r["identity_key"] or f"CLIENTE:{r['customer_id']}").strip()
        tipo = (r["identity_tipo"] or "CLIENTE").upper()
        customer_name = _safe_display_name(r["customer_nome_raw"], r["customer_fantasia_raw"], r["codigo"], r["customer_id"])
        identity_raw = (r["identity_nome_raw"] or "").strip()
        identity_name = identity_raw if identity_raw and not _is_generic_name(identity_raw) else customer_name
        matriz_id = int(r["matriz_customer_id"] or 0)
        matriz_nome = (r["matriz_nome"] or "").strip()
        if tipo == "MATRIZ" and (not matriz_nome or _is_generic_name(matriz_nome)):
            matriz_nome = customer_name
            matriz_id = int(r["customer_id"])

        item = entidades.get(ik)
        if item is None:
            entidades[ik] = {
                "identity_key": ik,
                "identity_nome": identity_name,
                "identity_tipo": tipo,
                "cliente_id": matriz_id or int(r["customer_id"]),
                "codigo": r["codigo"] or "",
                "matriz_nome": matriz_nome,
                "total_clientes": 1,
                "filiais_qtd": 0,
            }
        else:
            item["total_clientes"] += 1
            if prioridade.get(tipo, 9) < prioridade.get(item["identity_tipo"], 9):
                item["identity_tipo"] = tipo
                item["cliente_id"] = matriz_id or int(r["customer_id"])
                if matriz_nome:
                    item["matriz_nome"] = matriz_nome
                if tipo == "MATRIZ":
                    item["identity_nome"] = matriz_nome or identity_name
            elif not item.get("matriz_nome") and matriz_nome:
                item["matriz_nome"] = matriz_nome

    items = list(entidades.values())
    for item in items:
        total = int(item.get("total_clientes") or 0)
        tipo = (item.get("identity_tipo") or "CLIENTE").upper()
        if tipo == "REDE":
            item["filiais_qtd"] = total
        elif tipo == "CLIENTE":
            item["filiais_qtd"] = 0
        else:
            item["filiais_qtd"] = max(total - 1, 0)

    qq = (q or "").strip().upper()
    if qq:
        items = [x for x in items if any(qq in str(x.get(k) or "").upper() for k in (
            "identity_nome", "identity_tipo", "codigo", "matriz_nome", "identity_key"
        ))]

    ordem_tipo = {"MATRIZ": 0, "REDE": 1, "FILIAL": 2, "CLIENTE": 3}
    items.sort(key=lambda x: (ordem_tipo.get((x.get("identity_tipo") or "").upper(), 9), (x.get("identity_nome") or "").upper()))
    result = items[: max(1, int(limit or 300))]
    _customer_cache.set(key, result, ttl_seconds=max(30, CACHE_TTL_SECONDS), version=version)
    return result


def _allowed_customer_identity_map(legacy, conn, user=None, customer_ids_scope=None) -> dict[int, str]:
    role = ((user.get("role") if user else "") or "").strip().lower()
    vendor_lock = legacy.get_user_vendor_id(conn, user) if user else None
    params: list[Any] = []
    vendor_sql = ""
    if user and role not in ("admin", "gestor") and vendor_lock:
        vendor_sql = """
            AND EXISTS (
                SELECT 1 FROM invoices iv
                WHERE iv.cliente_id=c.id AND iv.vendor_id=?
            )
        """
        params.append(int(vendor_lock))

    rows = conn.execute(f"""
        SELECT c.id,
               COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || c.id) AS identity_key
        FROM customers c
        LEFT JOIN customer_identity_rules cir ON cir.customer_id=c.id
        WHERE 1=1 {vendor_sql}
    """, params).fetchall()
    out = {int(r["id"]): (r["identity_key"] or f"CLIENTE:{r['id']}") for r in rows}
    if customer_ids_scope:
        scope = {int(x) for x in customer_ids_scope if x}
        out = {cid: ik for cid, ik in out.items() if cid in scope}
    return out


def build_identity_score_map_fast(legacy, conn, user=None, customer_ids_scope=None):
    """Calcula score com apenas duas leituras em lote em vez de milhares de SELECTs."""
    version = _db_version()
    scope_sig = tuple(sorted(int(x) for x in (customer_ids_scope or []) if x))
    key = ("score_fast", _user_sig(user), scope_sig)
    cached = _score_cache.get(key, version=version)
    if cached is not None:
        return cached

    id_map = _allowed_customer_identity_map(legacy, conn, user=user, customer_ids_scope=customer_ids_scope)
    identities = set(id_map.values())
    raw: dict[str, dict[str, Any]] = {
        ik: {"valor_total": 0.0, "datas": []} for ik in identities
    }

    for r in conn.execute("""
        SELECT cliente_id, data_emissao, COALESCE(valor,0) AS valor
        FROM invoices
        WHERE cliente_id IS NOT NULL
          AND COALESCE(valor,0) > 0
          AND COALESCE(data_emissao,'') <> ''
        ORDER BY data_emissao ASC
    """):
        cid = int(r["cliente_id"] or 0)
        ik = id_map.get(cid)
        if not ik:
            continue
        st = raw.setdefault(ik, {"valor_total": 0.0, "datas": []})
        st["valor_total"] += float(r["valor"] or 0)
        d = legacy.try_parse_date(r["data_emissao"])
        if d:
            try:
                st["datas"].append(date.fromisoformat(d))
            except Exception:
                pass

    valor_max = max((float(v["valor_total"] or 0) for v in raw.values()), default=0.0)
    ref = date.today()
    result: dict[str, dict[str, Any]] = {}

    for ik, st in raw.items():
        datas = f_recompra.datas_unicas_ordenadas(st["datas"])
        intervalos = f_recompra.intervalos_entre_compras(datas)
        intervalo_mediano = float(median(intervalos)) if intervalos else None
        ultima_data = datas[-1] if datas else None
        atraso_atual = (ref - ultima_data).days if ultima_data else None
        stats = {
            "valor_total": float(st["valor_total"] or 0),
            "pedidos": len(datas),
            "ultima_data": ultima_data,
            "intervalo_mediano": intervalo_mediano,
            "atraso_atual": atraso_atual,
        }
        result[ik] = legacy.montar_score_identidade(stats, valor_max)

    _score_cache.set(key, result, ttl_seconds=max(30, CACHE_TTL_SECONDS), version=version)
    return result


def _build_identity_name_map(conn) -> tuple[dict[str, str], dict[int, str]]:
    identity_names: dict[str, str] = {}
    customer_names: dict[int, str] = {}
    rows = conn.execute("""
        SELECT c.id,
               COALESCE(c.codigo,'') AS codigo,
               COALESCE(c.nome,'') AS nome_raw,
               COALESCE(c.fantasia,'') AS fantasia_raw,
               COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || c.id) AS identity_key,
               COALESCE(cir.identity_nome,'') AS identity_nome_raw,
               UPPER(COALESCE(NULLIF(TRIM(cir.tipo_vinculo),''), 'CLIENTE')) AS tipo
        FROM customers c
        LEFT JOIN customer_identity_rules cir ON cir.customer_id=c.id
        ORDER BY CASE UPPER(COALESCE(cir.tipo_vinculo,'CLIENTE')) WHEN 'MATRIZ' THEN 0 WHEN 'REDE' THEN 1 WHEN 'FILIAL' THEN 2 ELSE 3 END, c.id
    """).fetchall()
    for r in rows:
        cid = int(r["id"])
        cname = _safe_display_name(r["nome_raw"], r["fantasia_raw"], r["codigo"], cid)
        customer_names[cid] = cname
        ik = (r["identity_key"] or f"CLIENTE:{cid}").strip()
        identity_raw = (r["identity_nome_raw"] or "").strip()
        iname = identity_raw if identity_raw and not _is_generic_name(identity_raw) else cname
        identity_names.setdefault(ik, iname)
    return identity_names, customer_names


def get_identity_display_name_fast(conn, identity_key: str, fallback_customer_id: Optional[int] = None) -> str:
    version = _db_version()
    cached = _identity_name_cache.get("names", version=version)
    if cached is None:
        cached = _build_identity_name_map(conn)
        _identity_name_cache.set("names", cached, ttl_seconds=max(30, CACHE_TTL_SECONDS), version=version)
    identity_names, customer_names = cached
    ik = (identity_key or "").strip()
    if ik and ik != "SEM_IDENTIDADE" and ik in identity_names:
        return identity_names[ik]
    if fallback_customer_id:
        return customer_names.get(int(fallback_customer_id), "Cliente")
    return "Cliente"


def _movement_map(conn, table: str, id_to_identity: dict[int, str]):
    out: dict[str, dict[str, Any]] = defaultdict(lambda: {"rows": 0, "days": defaultdict(float)})
    for r in conn.execute(f"""
        SELECT cliente_id, data_emissao, COALESCE(valor,0) AS valor
        FROM {table}
        WHERE cliente_id IS NOT NULL
          AND COALESCE(valor,0) > 0
          AND COALESCE(data_emissao,'') <> ''
        ORDER BY data_emissao ASC
    """):
        cid = int(r["cliente_id"] or 0)
        ik = id_to_identity.get(cid)
        if not ik:
            continue
        txt = str(r["data_emissao"] or "")[:10]
        dt = None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(txt, fmt)
                break
            except Exception:
                pass
        if not dt:
            continue
        dkey = dt.strftime("%Y-%m-%d")
        out[ik]["rows"] += 1
        out[ik]["days"][dkey] += float(r["valor"] or 0)
    return out


def _build_repurchase_map(conn) -> tuple[dict[str, dict[str, Any]], dict[int, str]]:
    rows = conn.execute("""
        SELECT c.id,
               COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || c.id) AS identity_key
        FROM customers c
        LEFT JOIN customer_identity_rules cir ON cir.customer_id=c.id
    """).fetchall()
    id_to_identity = {int(r["id"]): (r["identity_key"] or f"CLIENTE:{r['id']}") for r in rows}
    inv = _movement_map(conn, "invoices", id_to_identity)
    orders = _movement_map(conn, "orders", id_to_identity)
    identities = set(id_to_identity.values()) | set(inv.keys()) | set(orders.keys())
    result: dict[str, dict[str, Any]] = {}
    for ik in identities:
        source = inv.get(ik)
        if not source or int(source.get("rows", 0)) < 2:
            source = orders.get(ik, {"rows": 0, "days": {}})
        days = source.get("days", {}) if source else {}
        dates = sorted(datetime.strptime(d, "%Y-%m-%d") for d in days.keys())
        vals = [float(days[d.strftime("%Y-%m-%d")]) for d in dates]
        qtd = len(dates)
        ticket = (sum(vals) / qtd) if qtd else 0.0
        ultima = dates[-1] if dates else None
        media = None
        dias_fora = None
        atrasado = False
        if qtd >= 2:
            intervalos = f_recompra.intervalos_entre_compras([d.date() for d in dates])
            if intervalos:
                media = float(median(intervalos))
                dias_desde = (date.today() - ultima.date()).days if ultima else 0
                calc = int(round(dias_desde - media))
                dias_fora = max(0, calc)
                atrasado = f_recompra.esta_atrasado(dias_desde, media)
        result[ik] = {
            "ticket_medio": ticket,
            "dias_fora_media": dias_fora,
            "media_dias_recompra": media,
            "ultima_compra": ultima.strftime("%Y-%m-%d") if ultima else None,
            "qtd_compras": qtd,
            "atrasado": atrasado,
        }
    return result, id_to_identity


def calcular_metricas_recompra_fast(conn, cliente_id=None, identity_key=""):
    version = _db_version()
    cached = _repurchase_cache.get("repurchase", version=version)
    if cached is None:
        cached = _build_repurchase_map(conn)
        _repurchase_cache.set("repurchase", cached, ttl_seconds=max(30, CACHE_TTL_SECONDS), version=version)
    metrics_map, id_to_identity = cached
    ik = (identity_key or "").strip()
    if not ik and cliente_id:
        ik = id_to_identity.get(int(cliente_id), f"CLIENTE:{int(cliente_id)}")
    return metrics_map.get(ik, {
        "ticket_medio": 0.0,
        "dias_fora_media": None,
        "media_dias_recompra": None,
        "ultima_compra": None,
        "qtd_compras": 0,
        "atrasado": False,
    })



def _invoice_identity_history_fast(conn):
    """Histórico de faturamento por identidade comercial, carregado uma vez por versão do banco."""
    version = _db_version()
    cached = _invoice_history_cache.get("invoice_identity_history", version=version)
    if cached is not None:
        return cached

    dates_by_identity: dict[str, list[date]] = defaultdict(list)
    rows = conn.execute("""
        SELECT
            COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || i.cliente_id) AS identity_key,
            date(i.data_emissao) AS data_emissao
        FROM invoices i
        LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
        WHERE i.cliente_id IS NOT NULL
          AND i.data_emissao IS NOT NULL
          AND TRIM(COALESCE(i.data_emissao,'')) <> ''
        ORDER BY identity_key, date(i.data_emissao), i.id
    """).fetchall()
    for r in rows:
        ik = (r["identity_key"] or "").strip()
        ds = (r["data_emissao"] or "").strip()
        if not ik or not ds:
            continue
        try:
            d = date.fromisoformat(ds[:10])
        except Exception:
            continue
        dates_by_identity[ik].append(d)

    # Datas repetidas em notas diferentes continuam no histórico; para cálculo de frequência,
    # compras no mesmo dia representam um único movimento comercial.
    normalized = {ik: sorted(set(ds)) for ik, ds in dates_by_identity.items()}
    first_by_identity = {ik: ds[0] for ik, ds in normalized.items() if ds}
    payload = (normalized, first_by_identity)
    _invoice_history_cache.set("invoice_identity_history", payload, ttl_seconds=max(60, CACHE_TTL_SECONDS), version=version)
    return payload


def _first_invoice_doc_map_fast(conn):
    """Primeiro documento por identidade, cacheado pela versão do banco."""
    version = _db_version()
    cached = _first_invoice_doc_cache.get("first_invoice_docs", version=version)
    if cached is not None:
        return cached
    rows = conn.execute("""
        WITH ranked AS (
            SELECT
                i.id, i.numero, i.pedido, date(i.data_emissao) AS data_emissao,
                COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || i.cliente_id) AS identity_key,
                ROW_NUMBER() OVER (
                    PARTITION BY COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || i.cliente_id)
                    ORDER BY date(i.data_emissao), i.id
                ) AS rn
            FROM invoices i
            LEFT JOIN customer_identity_rules cir ON cir.customer_id=i.cliente_id
            WHERE i.cliente_id IS NOT NULL AND i.data_emissao IS NOT NULL
        )
        SELECT id,numero,pedido,data_emissao,identity_key FROM ranked WHERE rn=1
    """).fetchall()
    out = {(r["identity_key"] or "").strip(): dict(r) for r in rows if (r["identity_key"] or "").strip()}
    _first_invoice_doc_cache.set("first_invoice_docs", out, ttl_seconds=max(120, CACHE_TTL_SECONDS), version=version)
    return out


def _jit_orders_fast(conn):
    """Pedidos que contêm qualquer linha JIT, cacheados por versão do banco."""
    version = _db_version()
    cached = _jit_orders_cache.get("jit_orders", version=version)
    if cached is not None:
        return cached
    out = set()
    try:
        for r in conn.execute("""
            SELECT DISTINCT TRIM(COALESCE(pedido,'')) AS pedido
            FROM orders
            WHERE TRIM(COALESCE(pedido,''))<>''
              AND (COALESCE(is_jit,0)=1 OR UPPER(COALESCE(condicao_entrega,'')) LIKE '%JIT%')
        """).fetchall():
            ped = (r["pedido"] or "").strip()
            if ped:
                out.add(ped)
    except Exception:
        pass
    _jit_orders_cache.set("jit_orders", out, ttl_seconds=max(120, CACHE_TTL_SECONDS), version=version)
    return out


def build_vendor_period_mix_map_fast(legacy, conn, d0: date, d1: date):
    """Fonte CANÔNICA e em lote de NOVO / RECORRENTE / RECUPERADO.

    Usa FATURAMENTO/INVOICES e a mesma regra do Extrato de Comissão:
    identidade comercial atual, primeiro documento histórico, override manual e
    bloqueio de bônus NOVO para JIT. A Visão do Vendedor e a Performance leem
    exatamente este mesmo serviço; nenhuma tela recalcula por pedidos em paralelo.

    A contagem é por identidade única em cada classificação. Uma identidade que
    estreia como NOVO e volta a comprar no mesmo ciclo pode aparecer também em
    RECORRENTE, igual ao extrato financeiro do vendedor.
    """
    if not isinstance(d0, date):
        d0 = date.fromisoformat(str(d0)[:10])
    if not isinstance(d1, date):
        d1 = date.fromisoformat(str(d1)[:10])
    version = _db_version()
    key = ("vendor_period_mix_invoice_canonical", d0.isoformat(), d1.isoformat())
    cached = _vendor_period_mix_cache.get(key, version=version)
    if cached is not None:
        return copy.deepcopy(cached)

    cycle_rows = conn.execute("""
        SELECT
            i.id, i.numero, i.pedido, date(i.data_emissao) AS data_emissao,
            CAST(i.vendor_id AS INTEGER) AS vendor_id,
            CAST(i.cliente_id AS INTEGER) AS cliente_id,
            COALESCE(i.is_jit,0) AS is_jit,
            COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || i.cliente_id) AS identity_key,
            COALESCE(NULLIF(TRIM(cir.identity_nome),''), NULLIF(TRIM(c.fantasia),''), NULLIF(TRIM(c.nome),''), 'Cliente') AS identity_nome
        FROM invoices i
        LEFT JOIN customers c ON c.id=i.cliente_id
        LEFT JOIN customer_identity_rules cir ON cir.customer_id=i.cliente_id
        WHERE i.vendor_id IS NOT NULL
          AND i.cliente_id IS NOT NULL
          AND i.data_emissao IS NOT NULL
          AND date(i.data_emissao) BETWEEN date(?) AND date(?)
        ORDER BY date(i.data_emissao), i.id
    """, (d0.isoformat(), d1.isoformat())).fetchall()

    # Primeiro documento/histórico são cacheados por versão do banco e
    # reaproveitados em todos os meses do score anual.
    first_by_identity = _first_invoice_doc_map_fast(conn)

    history_by_identity, _first_dates = _invoice_identity_history_fast(conn)

    try:
        override_rows = conn.execute("SELECT identity_key, UPPER(TRIM(COALESCE(status_manual,''))) AS status_manual FROM customer_status_overrides").fetchall()
        overrides = {(r["identity_key"] or "").strip(): (r["status_manual"] or "").strip().upper() for r in override_rows}
    except Exception:
        overrides = {}

    # Mesmo conceito de invoice_tem_qualquer_jit(), cacheado em lote.
    jit_orders = _jit_orders_fast(conn)

    raw: dict[int, dict[str, Any]] = {}
    for r in cycle_rows:
        vid = int(r["vendor_id"] or 0)
        if vid <= 0:
            continue
        bucket = raw.setdefault(vid, {
            "novo_ids": set(), "rep_ids": set(), "recuperado_ids": set(),
            "entity_map": {},
        })
        ik = (r["identity_key"] or "").strip()
        if not ik:
            continue
        ds = (r["data_emissao"] or "").strip()
        first = first_by_identity.get(ik)
        current_doc = str(r["numero"] or r["pedido"] or f"ID:{r['id']}").strip()
        is_novo = False
        if first:
            first_date = (first["data_emissao"] or "").strip()
            first_doc = str(first["numero"] or first["pedido"] or f"ID:{first['id']}").strip()
            is_novo = bool(first_date and d0.isoformat() <= first_date <= d1.isoformat() and current_doc == first_doc)

        manual = overrides.get(ik, "")
        if manual == "NOVO":
            is_novo = True
        elif manual == "RECOMPRA":
            is_novo = False

        pedido = (r["pedido"] or "").strip()
        is_jit = bool(int(r["is_jit"] or 0)) or bool(pedido and pedido in jit_orders)
        if is_jit:
            is_novo = False

        is_recuperado = False
        if not is_novo and not is_jit and ds:
            try:
                data_mov = date.fromisoformat(ds[:10])
                hist = [d for d in history_by_identity.get(ik, []) if d < data_mov]
                is_recuperado = f_recompra.e_cliente_recuperado(data_mov, hist)
            except Exception:
                is_recuperado = False

        ent = bucket["entity_map"].setdefault(ik, {
            "identity_key": ik,
            "cliente_id": int(r["cliente_id"] or 0),
            "cliente": (r["identity_nome"] or "Cliente").strip(),
            "is_jit": bool(is_jit),
            "pedido_ref": pedido,
            "data_ref": ds,
            "is_novo": False,
            "is_recuperado": False,
            "primeiro_faturamento": (first["data_emissao"] if first else "") or "",
        })
        ent["is_novo"] = bool(ent["is_novo"] or is_novo)
        ent["is_recuperado"] = bool(ent["is_recuperado"] or is_recuperado)
        if not is_jit:
            ent["is_jit"] = False

    out: dict[int, dict[str, Any]] = {}
    for vid, bucket in raw.items():
        novos: set[str] = set()
        reps: set[str] = set()
        recups: set[str] = set()
        for ik, ent in bucket["entity_map"].items():
            if ent.get("is_novo"):
                novos.add(ik)
                continue
            reps.add(ik)
            if ent.get("is_recuperado"):
                recups.add(ik)
        out[vid] = {
            "novo_count": len(novos),
            "rep_count": len(reps),
            "recuperado_count": len(recups),
            "rep_regular_count": max(0, len(reps) - len(recups)),
            "entities": list(bucket["entity_map"].values()),
            "novos_nomes": sorted([bucket["entity_map"][ik]["cliente"] for ik in novos if ik in bucket["entity_map"]]),
            "recorrentes_nomes": sorted([bucket["entity_map"][ik]["cliente"] for ik in reps if ik in bucket["entity_map"]]),
            "recuperados_nomes": sorted([bucket["entity_map"][ik]["cliente"] for ik in recups if ik in bucket["entity_map"]]),
            "periodo_inicio": d0.isoformat(),
            "periodo_fim": d1.isoformat(),
        }

    _vendor_period_mix_cache.set(key, copy.deepcopy(out), ttl_seconds=max(60, CACHE_TTL_SECONDS), version=version)
    return out

def summarize_vendor_entities_in_period_fast(legacy, conn, vendor_id: int, d0: date, d1: date):
    mix = build_vendor_period_mix_map_fast(legacy, conn, d0, d1)
    return copy.deepcopy(mix.get(int(vendor_id or 0), {
        "novo_count": 0, "rep_count": 0, "recuperado_count": 0,
        "rep_regular_count": 0, "entities": []
    }))


def cached_performance_context_wrapper(original_func):
    """Cache por período/versão do banco. Retorna cópia para as rotas poderem enriquecer sem contaminar cache."""
    def wrapped(conn, data_ref, include_customer_mix=True, catalog=None):
        version = _db_version()
        key = ("performance_context", str(data_ref or ""), bool(include_customer_mix))
        cached = _performance_context_cache.get(key, version=version)
        if cached is not None:
            return copy.deepcopy(cached)
        value = original_func(conn, data_ref, include_customer_mix=include_customer_mix, catalog=catalog)
        _performance_context_cache.set(key, copy.deepcopy(value), ttl_seconds=max(60, CACHE_TTL_SECONDS), version=version)
        return value
    wrapped.__name__ = getattr(original_func, "__name__", "performance_context_cached")
    return wrapped


def cached_performance_annual_wrapper(original_func):
    def wrapped(conn, year, through_month, catalog=None):
        version = _db_version()
        key = ("performance_annual", int(year), int(through_month))
        cached = _performance_annual_cache.get(key, version=version)
        if cached is not None:
            return copy.deepcopy(cached)
        value = original_func(conn, year, through_month, catalog=catalog)
        _performance_annual_cache.set(key, copy.deepcopy(value), ttl_seconds=max(120, CACHE_TTL_SECONDS), version=version)
        return value
    wrapped.__name__ = getattr(original_func, "__name__", "performance_annual_cached")
    return wrapped

def cached_chart_wrapper(name: str, func):
    def wrapped(*args, **kwargs):
        try:
            payload = json.dumps([args, kwargs], ensure_ascii=False, default=str, sort_keys=True)
        except Exception:
            payload = repr((args, kwargs))
        digest = hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest()
        key = ("chart", name, digest)
        cached = _chart_cache.get(key, version="charts-v1")
        if cached is not None:
            return cached
        value = func(*args, **kwargs)
        _chart_cache.set(key, value, ttl_seconds=180, version="charts-v1")
        return value
    wrapped.__name__ = getattr(func, "__name__", name)
    return wrapped
