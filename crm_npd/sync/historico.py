"""Importador de dados historicos a partir de arquivos CSV.

Substitui a base historica anterior a data de corte (padrao: 2026-03-01)
por dados consolidados e validados, mantendo o Sisplan ativo dali em diante.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from crm_npd.config import HISTORICO_CORTE, HISTORICO_PEDIDOS_PATH
from crm_npd.migrations import apply_safe_migrations

logger = logging.getLogger(__name__)


def pad_pedido(p: str | None) -> str:
    """Padroniza numero de pedido com zeros a esquerda na base numerica."""
    p = str(p or "").strip()
    if not p:
        return ""
    base, sep, suf = p.partition("/")
    if base.isdigit():
        base = base.zfill(5)
    return f"{base}{sep}{suf}"


def parse_br_date(val: Any) -> str | None:
    """Converte dd/mm/aaaa para YYYY-MM-DD."""
    if val is None:
        return None
    val_str = str(val).strip()
    if not val_str:
        return None
    if "/" in val_str:
        parts = val_str.split("/")
        if len(parts) == 3:
            d, m, y = parts
            return f"{y.strip().zfill(4)}-{m.strip().zfill(2)}-{d.strip().zfill(2)}"
    if len(val_str) >= 10 and val_str[4] == "-" and val_str[7] == "-":
        return val_str[:10]
    return None


def parse_num(val: Any) -> float:
    """Converte string numerica brasileira ou float para float."""
    if val is None or val == "":
        return 0.0
    s = str(val).strip()
    if not s:
        return 0.0
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def obter_status_historico_pedidos(conn: sqlite3.Connection) -> dict[str, Any]:
    """Retorna informacoes do arquivo e da importacao do historico."""
    from crm_npd import legacy_monolith as m

    csv_path = Path(HISTORICO_PEDIDOS_PATH)
    existe = csv_path.is_file()
    mtime = None
    tamanho_bytes = 0
    if existe:
        try:
            stat = csv_path.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m/%Y %H:%M:%S")
            tamanho_bytes = stat.st_size
        except OSError:
            pass

    importado_em = m.get_setting(conn, "historico_pedidos_importado_em", "")
    linhas_importadas = m.get_setting(conn, "historico_pedidos_linhas", "")
    corte_registrado = m.get_setting(conn, "historico_pedidos_corte", HISTORICO_CORTE)

    qtd_no_banco = 0
    try:
        row = conn.execute("SELECT COUNT(1) FROM orders WHERE UPPER(TRIM(COALESCE(origem_ref,''))) = 'HISTORICO'").fetchone()
        qtd_no_banco = int(row[0] or 0) if row else 0
    except sqlite3.OperationalError:
        pass

    return {
        "arquivo_path": str(csv_path),
        "arquivo_existe": existe,
        "arquivo_mtime": mtime,
        "arquivo_tamanho_bytes": tamanho_bytes,
        "corte": corte_registrado or HISTORICO_CORTE,
        "importado_em": importado_em,
        "linhas_importadas": int(linhas_importadas) if linhas_importadas.isdigit() else 0,
        "pedidos_no_banco": qtd_no_banco,
    }


def importar_historico_pedidos(
    conn: sqlite3.Connection,
    path: Path | str | None = None,
    corte: str | None = None,
) -> dict[str, Any]:
    """Le historicopedidos.csv e grava na tabela orders com origem_ref='HISTORICO'.

    Substitui qualquer pedido do periodo anterior ao corte.
    """
    from crm_npd import legacy_monolith as m
    from crm_npd.sync.etl import _gravar_pedidos, aplicar_carteira_anfarma

    apply_safe_migrations(conn)

    csv_path = Path(path) if path else Path(HISTORICO_PEDIDOS_PATH)
    if not csv_path.is_file():
        raise FileNotFoundError(f"Arquivo de historico de pedidos nao encontrado em: {csv_path}")

    data_corte = str(corte or HISTORICO_CORTE or "2026-03-01").strip()

    raw_bytes = csv_path.read_bytes()
    decoded_txt = ""
    for enc in ("cp1252", "utf-8-sig", "latin-1", "utf-8"):
        try:
            decoded_txt = raw_bytes.decode(enc)
            break
        except Exception:
            continue

    if not decoded_txt:
        raise ValueError("Nao foi possivel decodificar o arquivo historicopedidos.csv")

    first_line = decoded_txt.splitlines()[0] if decoded_txt.splitlines() else ""
    delimiter = ";" if first_line.count(";") >= first_line.count(",") else ","

    reader = csv.DictReader(io.StringIO(decoded_txt), delimiter=delimiter)

    total_lidas = 0
    cliches_descartados = 0
    pos_corte_descartados = 0
    order_seq: dict[str, int] = {}
    rows_para_gravar: list[dict[str, Any]] = []

    for r in reader:
        total_lidas += 1
        grupo = str(r.get("GRUPO") or "").strip()
        if "CLICHE" in grupo.upper():
            cliches_descartados += 1
            continue

        emissao_iso = parse_br_date(r.get("EMISSAO_PEDIDO"))
        if not emissao_iso:
            continue
        if data_corte and emissao_iso >= data_corte:
            pos_corte_descartados += 1
            continue

        pedido = pad_pedido(r.get("PEDIDO"))
        if not pedido:
            continue

        order_seq[pedido] = order_seq.get(pedido, 0) + 1
        seq_str = str(order_seq[pedido])

        cliente_str = str(r.get("CLIENTE") or "").strip()
        codcli = ""
        cli_nome = cliente_str
        match_cli = re.match(r"^(\d+)\s*-\s*(.*)$", cliente_str)
        if match_cli:
            codcli = match_cli.group(1).strip()
            cli_nome = match_cli.group(2).strip()

        fantasia = str(r.get("FANTASIA") or "").strip() or cli_nome
        responsavel = str(r.get("RESPONSAVEL") or "").strip() or str(r.get("REPRESENTATE") or "").strip()

        entrega_iso = parse_br_date(r.get("ENTREGA_PEDIDO"))
        qtde = parse_num(r.get("QTDE_TOTAL"))
        preco = parse_num(r.get("PRECO"))
        valor_item = parse_num(r.get("VALOR_ITEM"))
        medida = str(r.get("MEDIDA") or "").strip()
        tipo_item = str(r.get("TIPO_ITEM_PEDIDO") or "").strip()
        papel = str(r.get("PAPEL") or "").strip()
        condicao = str(r.get("CONDICAO_ENTREGA") or "").strip()
        situacao = str(r.get("SITUACAO") or "").strip()
        motivo = str(r.get("MOTIVO") or "").strip()
        cidade = str(r.get("CIDADE") or "").strip()
        uf = str(r.get("UF") or "").strip().upper()
        produto_codigo = str(r.get("CODIGO") or "").strip()
        produto_ref = str(r.get("DESCRICAO") or "").strip()
        observacao = str(r.get("OBS") or "").strip()

        row_dict = {
            "PEDIDO": pedido,
            "DT_EMISSAO": emissao_iso,
            "ENTREGA_PEDIDO": entrega_iso,
            "CODCLI": codcli,
            "FANTASIA": fantasia,
            "CLI_NOME": cli_nome,
            "RESPONSAVEL": responsavel,
            "QTDE": qtde,
            "PRECO": preco,
            "VALOR_ITEM": valor_item,
            "MEDIDA": medida,
            "GRUPO": grupo,
            "TIPO_ITEM_PEDIDO": tipo_item,
            "PAPEL": papel,
            "CONDICAO_ENTREGA": condicao,
            "SITUACAO": situacao,
            "MOTIVO": motivo,
            "DESC_MOTIVO": "",
            "CIDADE": cidade,
            "UF": uf,
            "CODIGO": produto_codigo,
            "DESCRICAO": produto_ref,
            "OBS": observacao,
            "ORDEM": seq_str,
        }
        rows_para_gravar.append(row_dict)

    agora = m.now_iso()
    cache = m.build_import_entity_cache(conn)

    # Executa substituicao de forma atomica
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Apaga qualquer registro historico anterior ou registros sem origem antes do corte
        conn.execute(
            """
            DELETE FROM orders
            WHERE UPPER(TRIM(COALESCE(origem_ref, ''))) = 'HISTORICO'
               OR (COALESCE(origem_ref, '') = '' AND data_emissao < ?)
            """,
            (data_corte,),
        )

        # Marca registros existentes a partir do corte como SISPLAN se ainda nao tiverem origem
        conn.execute(
            """
            UPDATE orders
            SET origem_ref = 'SISPLAN'
            WHERE (origem_ref IS NULL OR TRIM(origem_ref) = '')
              AND data_emissao >= ?
            """,
            (data_corte,),
        )

        n_gravados = _gravar_pedidos(
            conn,
            m,
            rows_para_gravar,
            agora,
            cache,
            origem_ref="HISTORICO",
        )
        aplicar_carteira_anfarma(conn)

        m.set_setting(conn, "historico_pedidos_importado_em", agora)
        m.set_setting(conn, "historico_pedidos_linhas", str(n_gravados))
        m.set_setting(conn, "historico_pedidos_corte", data_corte)

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # Limpa caches em memoria
    try:
        m.clear_dashboard_cache()
    except Exception:
        pass

    resultado = {
        "status": "sucesso",
        "total_lidas": total_lidas,
        "cliches_descartados": cliches_descartados,
        "pos_corte_descartados": pos_corte_descartados,
        "pedidos_gravados": n_gravados,
        "corte": data_corte,
        "importado_em": agora,
    }
    logger.info("historico.pedidos.importado %s", resultado)
    return resultado
