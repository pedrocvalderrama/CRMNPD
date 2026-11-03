"""Importacao e consulta de notas fiscais emitidas pelo Mercado Livre (.xlsx).

Isolado em tabela propria (`mercado_livre_invoices`) para:
1. Nao ser apagado pelo `DELETE FROM invoices` da sincronizacao do Sisplan.
2. Nao contar em metas, ranking ou comissao de nenhum vendedor.
3. Permitir exibicao opcional no Dashboard do Admin (toggle "Mostrar Mercado Livre").
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import date, datetime
from io import BytesIO
from typing import Any

from openpyxl import load_workbook

_RE_QTD_ITEM = re.compile(r"\((\d+)\s*x\)", flags=re.IGNORECASE)


def _norm_header(texto: Any) -> str:
    bruto = str(texto or "").strip().lower()
    sem_acento = "".join(
        ch for ch in unicodedata.normalize("NFKD", bruto) if not unicodedata.combining(ch)
    )
    return re.sub(r"[^a-z0-9]+", " ", sem_acento).strip()


def _parse_float(val: Any) -> float:
    if val is None or val == "":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace("R$", "").replace(" ", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_data_emissao(val: Any) -> str | None:
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val.date().isoformat()
    if isinstance(val, date):
        return val.isoformat()
    texto = str(val).strip()
    if not texto:
        return None
    parte_data = re.split(r"[\sT]+", texto, maxsplit=1)[0].strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(parte_data, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _separar_cidade_uf(
    cidade_raw: Any = None,
    uf_raw: Any = None,
    cidade_uf_raw: Any = None,
) -> tuple[str, str]:
    cid = str(cidade_raw or "").strip()
    uf = str(uf_raw or "").strip().upper()
    combinado = str(cidade_uf_raw or "").strip()

    if cid and uf and cid.upper() != combinado.upper() and len(uf) == 2:
        return cid, uf

    fonte = combinado or cid
    if not fonte:
        return "", uf if len(uf) == 2 else ""

    for sep in (" - ", " / ", "/", "-"):
        if sep in fonte:
            esq, dir_uf = fonte.rsplit(sep, 1)
            dir_limpa = dir_uf.strip().upper()
            if len(dir_limpa) == 2 and dir_limpa.isalpha():
                return esq.strip(), dir_limpa

    return cid or fonte, uf if len(uf) == 2 else ""


def _fmt_br_date(iso_str: str | None) -> str:
    s = (iso_str or "").strip()[:10]
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return f"{s[8:10]}/{s[5:7]}/{s[0:4]}"
    return s or "-"


def _extrair_quantidade_itens(itens: str) -> float:
    if not itens:
        return 1.0
    matches = _RE_QTD_ITEM.findall(itens)
    if not matches:
        return 1.0
    total = sum(int(m) for m in matches)
    return float(total) if total > 0 else 1.0


def _tabela_existe(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mercado_livre_invoices'"
    ).fetchone()
    return row is not None


def ensure_mercado_livre_table(conn: sqlite3.Connection) -> None:
    """Garante a tabela dedicada de notas fiscais do Mercado Livre."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mercado_livre_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER,
            chave_acesso TEXT NOT NULL UNIQUE,
            numero_nfe TEXT NOT NULL,
            serie TEXT,
            data_emissao TEXT NOT NULL,
            status TEXT,
            tipo_nf TEXT DEFAULT 'VENDA',
            pedido_ml TEXT,
            destinatario TEXT,
            cpf_cnpj TEXT,
            cidade TEXT,
            uf TEXT,
            tipo_logistico TEXT,
            valor_produtos REAL DEFAULT 0,
            valor_frete REAL DEFAULT 0,
            valor_total REAL DEFAULT 0,
            quantidade REAL DEFAULT 1,
            itens TEXT,
            timestamp_log TEXT,
            imported_at TEXT
        )
        """
    )
    cols = {
        str(r[1]).lower()
        for r in conn.execute("PRAGMA table_info(mercado_livre_invoices)").fetchall()
    }
    if "timestamp_log" not in cols:
        conn.execute("ALTER TABLE mercado_livre_invoices ADD COLUMN timestamp_log TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ml_invoices_data ON mercado_livre_invoices(data_emissao)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ml_invoices_import ON mercado_livre_invoices(import_id)"
    )


def ler_planilha_mercado_livre(conteudo: bytes) -> tuple[int, list[dict[str, Any]]]:
    """Lê os bytes de um arquivo .xlsx de faturamento do Mercado Livre.

    Returns:
        Tupla `(total_linhas_dados, registros_validos)`.
    """
    if not conteudo:
        raise ValueError("Arquivo vazio.")
    try:
        wb = load_workbook(BytesIO(conteudo), data_only=True, read_only=True)
    except Exception as exc:
        raise ValueError(f"Arquivo Excel inválido: {exc}") from exc

    if not wb.sheetnames:
        raise ValueError("Planilha sem abas.")

    ws = wb["Notas Fiscais Emitidas"] if "Notas Fiscais Emitidas" in wb.sheetnames else wb[wb.sheetnames[0]]
    all_rows = list(ws.iter_rows(values_only=True))
    if not all_rows:
        raise ValueError("Planilha vazia.")

    header_idx: int | None = None
    col_map: dict[str, int] = {}
    for idx, row in enumerate(all_rows[:15]):
        normed = [_norm_header(c) for c in (row or ())]
        if any("numero nf" in h or "chave de acesso" in h or "pedido ml" in h for h in normed):
            header_idx = idx
            for c_idx, h in enumerate(normed):
                if h and h not in col_map:
                    col_map[h] = c_idx
            break

    if header_idx is None:
        raise ValueError(
            "Cabeçalho do relatório do Mercado Livre não reconhecido (esperado: Número NF-e, Data Emissão da NF-e, Valor Total NF-e)."
        )

    def _find_col(*prefixes: str) -> int | None:
        for p in prefixes:
            for h, c_idx in col_map.items():
                if h == p or h.startswith(p) or p in h:
                    return c_idx
        return None

    c_status = _find_col("status")
    c_numero = _find_col("numero nf", "nfe", "nf")
    c_serie = _find_col("serie")
    c_data = _find_col("data emissao", "emissao")
    c_chave = _find_col("chave de acesso", "chave")
    c_pedido = _find_col("pedido ml", "pedido")
    c_dest = _find_col("destinatario", "cliente")
    c_doc = _find_col("cpf cnpj", "cpf", "cnpj")
    c_cidade = _find_col("cidade", "municipio")
    c_uf = _find_col("uf", "estado")
    c_log = _find_col("tipo logistico", "logistica")
    c_vprod = _find_col("valor produtos")
    c_vfrete = _find_col("frete")
    c_vtot = _find_col("valor total nf", "valor total")
    c_itens = _find_col("itens", "produto", "descricao")

    if c_numero is None or c_data is None or (c_vtot is None and c_vprod is None):
        raise ValueError(
            "Colunas obrigatórias ausentes (Número NF-e, Data Emissão da NF-e e Valor Total NF-e)."
        )

    def _cell(row: tuple, col: int | None) -> Any:
        if col is None or col >= len(row):
            return None
        return row[col]

    linhas_dados = 0
    registros: list[dict[str, Any]] = []
    for row in all_rows[header_idx + 1 :]:
        if not row or not any(v is not None and str(v).strip() != "" for v in row):
            continue
        first_txt = str(row[0] or "").strip().upper()
        if first_txt.startswith("TOTAL"):
            continue

        linhas_dados += 1
        status_raw = str(_cell(row, c_status) or "Autorizada").strip()
        status_up = _norm_header(status_raw).upper()
        if status_up.startswith(("CANCEL", "DENEG", "REJEIT", "INUTIL")):
            continue

        numero_nfe = str(_cell(row, c_numero) or "").strip()
        data_emissao = _parse_data_emissao(_cell(row, c_data))
        if not numero_nfe or not data_emissao:
            continue

        serie = str(_cell(row, c_serie) or "").strip()
        chave_raw = re.sub(r"\s+", "", str(_cell(row, c_chave) or "").strip())
        chave_acesso = chave_raw or f"ML:{serie or '0'}:{numero_nfe}:{data_emissao}"

        eh_devolucao = status_up.startswith(("DEVOL", "ESTORN"))
        v_prod = abs(_parse_float(_cell(row, c_vprod)))
        v_frete = abs(_parse_float(_cell(row, c_vfrete)))
        v_tot_raw = _parse_float(_cell(row, c_vtot)) if c_vtot is not None else (v_prod + v_frete)
        v_tot = abs(v_tot_raw) if v_tot_raw != 0 else (v_prod + v_frete)

        if eh_devolucao or v_tot_raw < 0:
            v_prod = -v_prod
            v_frete = -v_frete
            v_tot = -abs(v_tot)
            tipo_nf = "DEVOLUÇÃO"
        else:
            tipo_nf = "VENDA"

        itens_str = str(_cell(row, c_itens) or "").strip()
        cidade_limpa, uf_limpa = _separar_cidade_uf(
            _cell(row, c_cidade),
            _cell(row, c_uf) if c_uf != c_cidade else None,
            _cell(row, c_cidade) if c_uf == c_cidade else None,
        )
        registros.append(
            {
                "chave_acesso": chave_acesso,
                "numero_nfe": numero_nfe,
                "serie": serie,
                "data_emissao": data_emissao,
                "status": status_raw,
                "tipo_nf": tipo_nf,
                "pedido_ml": str(_cell(row, c_pedido) or "").strip(),
                "destinatario": str(_cell(row, c_dest) or "").strip(),
                "cpf_cnpj": str(_cell(row, c_doc) or "").strip(),
                "cidade": cidade_limpa,
                "uf": uf_limpa,
                "tipo_logistico": str(_cell(row, c_log) or "").strip(),
                "valor_produtos": round(v_prod, 2),
                "valor_frete": round(v_frete, 2),
                "valor_total": round(v_tot, 2),
                "quantidade": _extrair_quantidade_itens(itens_str),
                "itens": itens_str,
                "timestamp_log": None,
            }
        )

    if not registros:
        raise ValueError("Nenhuma nota fiscal autorizada foi encontrada na planilha.")

    return linhas_dados, registros


def normalizar_nota_sheets_ml(inv: dict[str, Any]) -> dict[str, Any] | None:
    """Converte um registro JSON da planilha `notas_ml` (Apps Script) para o formato do CRM.

    Args:
        inv: Dicionário retornado na lista `invoices` do endpoint `?action=changes`.

    Returns:
        Dicionário normalizado (com `_cancelada=True` quando o status indicar
        cancelamento/denegação para remover eventual nota já gravada), ou `None`
        quando a linha estiver sem número/data identificáveis.
    """
    if not isinstance(inv, dict):
        return None

    numero_nfe = str(inv.get("numero_nfe") or inv.get("numero") or "").strip()
    data_emissao = _parse_data_emissao(inv.get("data_emissao"))
    serie = str(inv.get("serie") or inv.get("serie_nfe") or "").strip()
    chave_raw = re.sub(
        r"\s+",
        "",
        str(inv.get("chave") or inv.get("chave_nfe") or inv.get("chave_acesso") or "").strip(),
    )
    if not chave_raw and (not numero_nfe or not data_emissao):
        return None
    chave_acesso = chave_raw or f"ML:{serie or '0'}:{numero_nfe}:{data_emissao}"

    status_raw = str(inv.get("status") or "Autorizada").strip()
    status_up = _norm_header(status_raw).upper()
    if status_up.startswith(("CANCEL", "DENEG", "REJEIT", "INUTIL")):
        return {
            "_cancelada": True,
            "chave_acesso": chave_acesso,
            "numero_nfe": numero_nfe,
            "timestamp_log": str(inv.get("timestamp_log") or "").strip() or None,
        }

    if not numero_nfe or not data_emissao:
        return None

    eh_devolucao = status_up.startswith(("DEVOL", "ESTORN"))
    v_prod = abs(_parse_float(inv.get("valor_produtos")))
    frete_val = inv.get("frete") if inv.get("frete") is not None else inv.get("valor_frete")
    v_frete = abs(_parse_float(frete_val))
    v_tot_raw = (
        _parse_float(inv.get("valor_total"))
        if inv.get("valor_total") is not None
        else (v_prod + v_frete)
    )
    v_tot = abs(v_tot_raw) if v_tot_raw != 0 else (v_prod + v_frete)

    if eh_devolucao or v_tot_raw < 0:
        v_prod = -v_prod
        v_frete = -v_frete
        v_tot = -abs(v_tot)
        tipo_nf = "DEVOLUÇÃO"
    else:
        tipo_nf = "VENDA"

    itens_str = str(inv.get("itens") or "").strip()
    cidade_limpa, uf_limpa = _separar_cidade_uf(
        inv.get("cidade"),
        inv.get("uf"),
        inv.get("cidade_uf"),
    )
    pedido_ml = str(
        inv.get("order_id") or inv.get("pedido_ml") or inv.get("pedido") or ""
    ).strip()
    destinatario = str(
        inv.get("buyer_name") or inv.get("cliente_nome") or inv.get("destinatario") or ""
    ).strip()
    cpf_cnpj = str(
        inv.get("buyer_doc") or inv.get("cliente_doc") or inv.get("cpf_cnpj") or ""
    ).strip()
    tipo_logistico = str(
        inv.get("logistic_type") or inv.get("logistica") or inv.get("tipo_logistico") or ""
    ).strip()
    ts_log = str(inv.get("timestamp_log") or "").strip() or None

    return {
        "_cancelada": False,
        "chave_acesso": chave_acesso,
        "numero_nfe": numero_nfe,
        "serie": serie,
        "data_emissao": data_emissao,
        "status": status_raw,
        "tipo_nf": tipo_nf,
        "pedido_ml": pedido_ml,
        "destinatario": destinatario,
        "cpf_cnpj": cpf_cnpj,
        "cidade": cidade_limpa,
        "uf": uf_limpa,
        "tipo_logistico": tipo_logistico,
        "valor_produtos": round(v_prod, 2),
        "valor_frete": round(v_frete, 2),
        "valor_total": round(v_tot, 2),
        "quantidade": _extrair_quantidade_itens(itens_str),
        "itens": itens_str,
        "timestamp_log": ts_log,
    }


def aplicar_lote_notas_ml(
    conn: sqlite3.Connection,
    registros: list[dict[str, Any]],
    imported_at: str,
    *,
    import_id: int | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Aplica upsert/remoção de notas do Mercado Livre por `chave_acesso`."""
    ensure_mercado_livre_table(conn)

    por_chave: dict[str, dict[str, Any]] = {}
    duplicados_lote = 0
    for reg in registros:
        ch = str(reg.get("chave_acesso") or "").strip()
        if not ch:
            continue
        if ch in por_chave:
            duplicados_lote += 1
        por_chave[ch] = reg

    ativos = [r for r in por_chave.values() if not r.get("_cancelada")]
    canceladas = [r["chave_acesso"] for r in por_chave.values() if r.get("_cancelada")]

    removidos = 0
    for i in range(0, len(canceladas), 400):
        bloco_canc = canceladas[i : i + 400]
        placeholders = ",".join("?" for _ in bloco_canc)
        cur_del = conn.execute(
            f"DELETE FROM mercado_livre_invoices WHERE chave_acesso IN ({placeholders})",
            tuple(bloco_canc),
        )
        removidos += max(0, int(cur_del.rowcount or 0))

    chaves = [r["chave_acesso"] for r in ativos]
    existentes: set[str] = set()
    for i in range(0, len(chaves), 400):
        bloco = chaves[i : i + 400]
        placeholders = ",".join("?" for _ in bloco)
        rows_ex = conn.execute(
            f"SELECT chave_acesso FROM mercado_livre_invoices WHERE chave_acesso IN ({placeholders})",
            tuple(bloco),
        ).fetchall()
        existentes.update(str(r[0]) for r in rows_ex)

    novos = sum(1 for ch in chaves if ch not in existentes)
    substituidos = sum(1 for ch in chaves if ch in existentes)

    if ativos:
        conn.executemany(
            """
            INSERT INTO mercado_livre_invoices(
                import_id, chave_acesso, numero_nfe, serie, data_emissao,
                status, tipo_nf, pedido_ml, destinatario, cpf_cnpj,
                cidade, uf, tipo_logistico, valor_produtos, valor_frete,
                valor_total, quantidade, itens, timestamp_log, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chave_acesso) DO UPDATE SET
                import_id = COALESCE(excluded.import_id, mercado_livre_invoices.import_id),
                numero_nfe = excluded.numero_nfe,
                serie = excluded.serie,
                data_emissao = excluded.data_emissao,
                status = excluded.status,
                tipo_nf = excluded.tipo_nf,
                pedido_ml = excluded.pedido_ml,
                destinatario = excluded.destinatario,
                cpf_cnpj = excluded.cpf_cnpj,
                cidade = excluded.cidade,
                uf = excluded.uf,
                tipo_logistico = excluded.tipo_logistico,
                valor_produtos = excluded.valor_produtos,
                valor_frete = excluded.valor_frete,
                valor_total = excluded.valor_total,
                quantidade = excluded.quantidade,
                itens = excluded.itens,
                timestamp_log = COALESCE(excluded.timestamp_log, mercado_livre_invoices.timestamp_log),
                imported_at = excluded.imported_at
            """,
            [
                (
                    import_id,
                    r["chave_acesso"],
                    r["numero_nfe"],
                    r.get("serie") or None,
                    r["data_emissao"],
                    r.get("status") or None,
                    r["tipo_nf"],
                    r.get("pedido_ml") or None,
                    r.get("destinatario") or None,
                    r.get("cpf_cnpj") or None,
                    r.get("cidade") or None,
                    r.get("uf") or None,
                    r.get("tipo_logistico") or None,
                    r["valor_produtos"],
                    r["valor_frete"],
                    r["valor_total"],
                    r["quantidade"],
                    r.get("itens") or None,
                    r.get("timestamp_log") or None,
                    imported_at,
                )
                for r in ativos
            ],
        )

    if commit:
        conn.commit()

    datas = sorted(r["data_emissao"] for r in ativos if r.get("data_emissao"))
    return {
        "registros": len(ativos),
        "novos": novos,
        "substituidos": substituidos,
        "removidos": removidos,
        "duplicados_arquivo": duplicados_lote,
        "min_data": datas[0] if datas else None,
        "max_data": datas[-1] if datas else None,
        "valor_total": round(sum(float(r["valor_total"]) for r in ativos), 2),
    }


def importar_planilha_mercado_livre(
    conn: sqlite3.Connection,
    arquivo_nome: str,
    conteudo: bytes,
    imported_at: str,
) -> dict[str, Any]:
    """Importa planilha `.xlsx` do Mercado Livre para `mercado_livre_invoices`."""
    linhas_dados, registros = ler_planilha_mercado_livre(conteudo)
    ensure_mercado_livre_table(conn)

    cur = conn.execute(
        """
        INSERT INTO imports(
            tipo, arquivo, imported_at, linhas, registros,
            novos, substituidos, duplicados_arquivo
        ) VALUES (?, ?, ?, ?, 0, 0, 0, 0)
        """,
        (
            "MERCADO_LIVRE",
            arquivo_nome or "mercado_livre.xlsx",
            imported_at,
            linhas_dados,
        ),
    )
    import_id = int(cur.lastrowid or 0)
    res_lote = aplicar_lote_notas_ml(
        conn,
        registros,
        imported_at,
        import_id=import_id,
        commit=False,
    )
    conn.execute(
        """
        UPDATE imports
           SET registros = ?,
               novos = ?,
               substituidos = ?,
               duplicados_arquivo = ?
         WHERE id = ?
        """,
        (
            res_lote["registros"],
            res_lote["novos"],
            res_lote["substituidos"],
            res_lote["duplicados_arquivo"],
            import_id,
        ),
    )
    conn.commit()

    return {
        "import_id": import_id,
        "linhas": linhas_dados,
        **res_lote,
    }


def resumo_mercado_livre_periodo(
    conn: sqlite3.Connection, d0: date, d1: date
) -> dict[str, Any]:
    """Retorna os totais do Mercado Livre (com frete) no intervalo `[d0, d1]`."""
    vazio = {
        "valor_total": 0.0,
        "valor_produtos": 0.0,
        "valor_frete": 0.0,
        "valor_bruto": 0.0,
        "valor_devolvido": 0.0,
        "qtd_notas": 0,
        "qtd_full": 0,
        "qtd_envios": 0,
        "ticket_medio": 0.0,
    }
    if not _tabela_existe(conn):
        return vazio

    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(COALESCE(valor_total, 0)), 0) AS valor_total,
            COALESCE(SUM(COALESCE(valor_produtos, 0)), 0) AS valor_produtos,
            COALESCE(SUM(COALESCE(valor_frete, 0)), 0) AS valor_frete,
            COALESCE(SUM(CASE WHEN COALESCE(valor_total, 0) > 0 THEN valor_total ELSE 0 END), 0) AS valor_bruto,
            COALESCE(SUM(CASE WHEN COALESCE(valor_total, 0) < 0 THEN ABS(valor_total) ELSE 0 END), 0) AS valor_devolvido,
            COUNT(*) AS qtd_notas,
            COALESCE(SUM(CASE WHEN LOWER(COALESCE(tipo_logistico, '')) LIKE '%fulfill%' THEN 1 ELSE 0 END), 0) AS qtd_full
        FROM mercado_livre_invoices
        WHERE data_emissao IS NOT NULL
          AND date(data_emissao) BETWEEN date(?) AND date(?)
        """,
        (d0.isoformat(), d1.isoformat()),
    ).fetchone()
    if not row:
        return vazio

    valor_total = float(row["valor_total"] or 0.0)
    qtd_notas = int(row["qtd_notas"] or 0)
    qtd_full = int(row["qtd_full"] or 0)
    qtd_envios = max(0, qtd_notas - qtd_full)
    return {
        "valor_total": valor_total,
        "valor_produtos": float(row["valor_produtos"] or 0.0),
        "valor_frete": float(row["valor_frete"] or 0.0),
        "valor_bruto": float(row["valor_bruto"] or 0.0),
        "valor_devolvido": float(row["valor_devolvido"] or 0.0),
        "qtd_notas": qtd_notas,
        "qtd_full": qtd_full,
        "qtd_envios": qtd_envios,
        "ticket_medio": (valor_total / qtd_notas) if qtd_notas > 0 else 0.0,
    }


def listar_faturamento_mercado_livre_periodo(
    conn: sqlite3.Connection,
    d0: date,
    d1: date,
    include_items: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Lista notas do Mercado Livre no mesmo formato de `_query_dashboard_faturamento_periodo`."""
    if not _tabela_existe(conn):
        return [], []

    rows = conn.execute(
        """
        SELECT
            date(data_emissao) AS data_emissao,
            numero_nfe,
            serie,
            COALESCE(tipo_nf, 'VENDA') AS tipo_nf,
            COALESCE(pedido_ml, '') AS pedido_ml,
            COALESCE(destinatario, '') AS destinatario,
            COALESCE(tipo_logistico, '') AS tipo_logistico,
            COALESCE(itens, '') AS itens,
            COALESCE(quantidade, 1) AS quantidade,
            COALESCE(valor_total, 0) AS valor_total
        FROM mercado_livre_invoices
        WHERE data_emissao IS NOT NULL
          AND date(data_emissao) BETWEEN date(?) AND date(?)
        ORDER BY date(data_emissao) DESC, CAST(numero_nfe AS INTEGER) DESC, id DESC
        """,
        (d0.isoformat(), d1.isoformat()),
    ).fetchall()

    nf_rows: list[dict[str, Any]] = []
    item_rows: list[dict[str, Any]] = []
    for r in rows:
        dt_iso = str(r["data_emissao"] or "")
        nf_label = f"ML-{r['numero_nfe']}" if r["numero_nfe"] else "ML"
        tipo_nf = str(r["tipo_nf"] or "VENDA")
        pedido = str(r["pedido_ml"] or "").strip() or "-"
        cliente = str(r["destinatario"] or "").strip() or "-"
        itens = str(r["itens"] or "").strip() or "MERCADO LIVRE"
        qtd = float(r["quantidade"] or 1.0)
        val = float(r["valor_total"] or 0.0)
        log_raw = str(r["tipo_logistico"] or "").strip()
        grupo_ml = f"MERCADO LIVRE ({log_raw})" if log_raw else "MERCADO LIVRE"

        nf_rows.append(
            {
                "data_emissao": dt_iso,
                "data_fmt": _fmt_br_date(dt_iso),
                "nf": nf_label,
                "tipo_nf": tipo_nf,
                "pedido": pedido,
                "cliente_id": None,
                "cliente": cliente,
                "vendor_id": None,
                "vendedor": "MERCADO LIVRE",
                "canal": "MERCADO LIVRE",
                "grupo_produto": itens,
                "quantidade": qtd,
                "valor": val,
            }
        )
        if include_items:
            item_rows.append(
                {
                    "data_emissao": dt_iso,
                    "nf": nf_label,
                    "tipo_nf": tipo_nf,
                    "pedido": "" if pedido == "-" else pedido,
                    "cliente": cliente,
                    "vendedor": "MERCADO LIVRE",
                    "canal": "MERCADO LIVRE",
                    "grupo": grupo_ml,
                    "produto": itens,
                    "formato": "",
                    "papel": "",
                    "quantidade": qtd,
                    "valor": val,
                }
            )

    return nf_rows, item_rows
