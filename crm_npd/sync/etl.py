"""Copia SELECT-only do Sisplan para as tabelas de fato do CRM.

Substitui pedidos, faturamento, orcamentos e liquidez. Nao grava no Firebird.
Cadastros do CRM (usuarios, metas, tarefas, identidades manuais) permanecem.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date, datetime
from typing import Any

CODIGO_ANFARMA = "05975"
CODIGO_JOSEANE = "12667"
LABEL_JOSEANE = "12667 - JOSEANE VOLINGER DOS SANTOS"

from crm_npd.domain.status_os import rotulo_status_os
from crm_npd.repositories.firebird.conexao import (
    descricao_conexao,
    disponivel,
    executar_select_dicts,
)
from crm_npd.repositories.firebird.consultas import (
    CRM_FATURAMENTO,
    CRM_FATURAMENTO_DEMO,
    CRM_FATURAMENTO_ENTRADA,
    CRM_LIQUIDEZ,
    CRM_ORCAMENTOS,
    CRM_PEDIDOS,
    anexar_and,
    in_trim,
)

logger = logging.getLogger(__name__)


def sincronizar_crm(conn: sqlite3.Connection) -> dict[str, int]:
    """Le o FDB e regrava as quatro tabelas de fato.

    Args:
        conn: Conexao SQLite do CRM (escrita permitida aqui, nao no Firebird).

    Returns:
        Contagem de linhas gravadas por origem.

    Raises:
        RuntimeError: Se o Firebird nao estiver configurado.
    """
    if not disponivel():
        raise RuntimeError(
            "Firebird indisponivel. Configure SISPLAN_HOST / SISPLAN_PORT / "
            "SISPLAN_DATABASE / SISPLAN_PASSWORD no .env (manual de acesso)."
        )

    from crm_npd import legacy_monolith as m

    logger.info("sync.inicio %s", descricao_conexao())
    pedidos = executar_select_dicts(CRM_PEDIDOS)
    faturamento = _ler_faturamento()
    orcamentos = executar_select_dicts(CRM_ORCAMENTOS)
    liquidez = executar_select_dicts(CRM_LIQUIDEZ)
    logger.info(
        "sync.lidos pedidos=%s faturamento=%s orcamentos=%s liquidez=%s",
        len(pedidos), len(faturamento), len(orcamentos), len(liquidez),
    )

    agora = m.now_iso()
    conn.execute("BEGIN IMMEDIATE")
    for tabela in ("orders", "invoices", "orcamentos", "liquidez"):
        conn.execute(f"DELETE FROM {tabela}")
    try:
        conn.execute(
            "DELETE FROM import_identity_pending WHERE COALESCE(status,'PENDENTE')='PENDENTE'"
        )
    except sqlite3.Error:
        pass

    cache = m.build_import_entity_cache(conn)
    n_ped = _gravar_pedidos(conn, m, pedidos, agora, cache)
    n_fat = _gravar_faturamento(conn, m, faturamento, agora, cache)
    aplicar_carteira_anfarma(conn)
    n_orc = _gravar_orcamentos(conn, m, orcamentos, agora)
    n_liq = _gravar_liquidez(conn, m, liquidez, agora, cache)

    conn.execute(
        "INSERT INTO imports(tipo,arquivo,imported_at,linhas,registros) VALUES(?,?,?,?,?)",
        ("FIREBIRD", descricao_conexao(), agora, n_ped + n_fat + n_orc + n_liq, n_ped + n_fat + n_orc + n_liq),
    )

    m.set_setting(conn, "sisplan_origem", "FIREBIRD")
    m.set_setting(conn, "sisplan_snapshot_synced_at", agora)
    m.set_setting(conn, "sisplan_database", descricao_conexao())
    conn.commit()

    try:
        from crm_npd.sync.cdc import alinhar_checkpoint_ao_sisplan

        alinhar_checkpoint_ao_sisplan(conn)
        conn.commit()
    except Exception:
        logger.exception("cdc.checkpoint nao alinhado apos sync; o ouvinte faz cold start")

    try:
        m.repair_generic_customer_names(conn)
        m.sync_identity_rules_from_jit_history(conn)
        m.refresh_repurchase_tasks_after_sisplan(conn, actor_name="Sistema")
        conn.commit()
    except Exception:
        logger.exception("sync.pos falhou (fatos ja gravados)")

    lidas = {
        "pedidos": n_ped,
        "faturamento": n_fat,
        "orcamentos": n_orc,
        "liquidez": n_liq,
    }
    logger.info("sync.fim %s", lidas)
    return lidas


def _ler_faturamento() -> list[dict]:
    """Fonte A (NF saida) + B (devolucao entrada) + C (DEMO), sem frete nem cliche."""
    saida = executar_select_dicts(CRM_FATURAMENTO)
    entrada = executar_select_dicts(CRM_FATURAMENTO_ENTRADA)
    demo = executar_select_dicts(CRM_FATURAMENTO_DEMO)
    logger.info(
        "sync.faturamento.fontes saida=%s entrada_dev=%s demo=%s",
        len(saida), len(entrada), len(demo),
    )
    return saida + entrada + demo


def sincronizar_faturamento(conn: sqlite3.Connection) -> int:
    """Apaga e regrava so a tabela invoices, pelas regras do manual de faturamento."""
    if not disponivel():
        raise RuntimeError(
            "Firebird indisponivel. Configure SISPLAN_HOST / SISPLAN_PORT / "
            "SISPLAN_DATABASE / SISPLAN_PASSWORD no .env (manual de acesso)."
        )

    from crm_npd import legacy_monolith as m

    logger.info("sync.faturamento.inicio %s", descricao_conexao())
    rows = _ler_faturamento()
    agora = m.now_iso()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM invoices")
    cache = m.build_import_entity_cache(conn)
    n_fat = _gravar_faturamento(conn, m, rows, agora, cache)
    aplicar_carteira_anfarma(conn)
    m.set_setting(conn, "sisplan_snapshot_synced_at", agora)
    conn.commit()
    logger.info("sync.faturamento.fim %s", n_fat)
    return n_fat


def sincronizar_pedidos(conn: sqlite3.Connection) -> int:
    """Apaga e regrava so a tabela orders, pelas regras do manual de pedidos."""
    if not disponivel():
        raise RuntimeError(
            "Firebird indisponivel. Configure SISPLAN_HOST / SISPLAN_PORT / "
            "SISPLAN_DATABASE / SISPLAN_PASSWORD no .env (manual de acesso)."
        )

    from crm_npd import legacy_monolith as m

    logger.info("sync.pedidos.inicio %s", descricao_conexao())
    pedidos = executar_select_dicts(CRM_PEDIDOS)
    agora = m.now_iso()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM orders")
    cache = m.build_import_entity_cache(conn)
    n_ped = _gravar_pedidos(conn, m, pedidos, agora, cache)
    aplicar_carteira_anfarma(conn)
    m.set_setting(conn, "sisplan_snapshot_synced_at", agora)
    conn.commit()
    logger.info("sync.pedidos.fim %s", n_ped)
    return n_ped


def sincronizar_periodo(conn: sqlite3.Connection, inicio: date, fim: date) -> dict[str, int]:
    """Mantido por compatibilidade: a carga oficial ignora o recorte e le o FDB."""
    return sincronizar_crm(conn)


def _data(valor: Any) -> str | None:
    if valor is None:
        return None
    if isinstance(valor, datetime):
        texto = valor.date().isoformat()
    elif isinstance(valor, date):
        texto = valor.isoformat()
    else:
        texto = str(valor).strip()[:10]
    if len(texto) < 10 or texto < "1901-01-01":
        return None
    return texto


def _num(valor: Any) -> float:
    if valor is None or valor == "":
        return 0.0
    try:
        return float(valor)
    except (TypeError, ValueError):
        return 0.0


def _txt(valor: Any) -> str:
    return str(valor or "").strip()


def _rede_key(codigo: str) -> str:
    codigo = (codigo or "").strip()
    return f"GRUPO:{codigo}" if codigo else ""


def eh_responsavel_anfarma(texto: str | None) -> bool:
    """True se o responsavel e a Associacao Nacional (codigo 05975)."""
    bruto = _txt(texto).upper()
    if not bruto:
        return False
    match = re.match(r"^(\d+)", bruto)
    if match and match.group(1).lstrip("0").zfill(5) == CODIGO_ANFARMA:
        return True
    return "ASSOCIACAO NACIONAL DE FARMACEUTICOS" in bruto


def carteira_do_responsavel(texto: str | None) -> str:
    """ANFARMA (05975) entra na carteira da Joseane (12667)."""
    origem = _txt(texto)
    if eh_responsavel_anfarma(origem):
        return LABEL_JOSEANE
    return origem


def vendedor_pedido(row: dict) -> str:
    """Pedidos entram na carteira do RESPONSAVEL, nao do representante."""
    return (
        _txt(row.get("RESPONSAVEL"))
        or _txt(row.get("RESPONSAVEL_NOME"))
        or _txt(row.get("REP_NOME"))
    )


def vendedor_faturamento(row: dict) -> str:
    """NF entra na carteira do RESPONSAVEL do pedido, nao do representante da nota."""
    return vendedor_pedido(row)


def aplicar_carteira_anfarma(conn: sqlite3.Connection) -> int:
    """Move pedidos e NFs do codigo 05975 para a Joseane, sem apagar a origem."""
    from crm_npd import legacy_monolith as m

    joseane_id = m.upsert_vendor(conn, LABEL_JOSEANE)
    if not joseane_id:
        return 0
    anfarma_ids = [
        int(row["id"])
        for row in conn.execute(
            "SELECT id FROM vendors WHERE TRIM(COALESCE(codigo,''))=?",
            (CODIGO_ANFARMA,),
        ).fetchall()
    ]
    movidos = 0
    if anfarma_ids:
        marks = ",".join("?" for _ in anfarma_ids)
        movidos += conn.execute(
            f"UPDATE orders SET vendor_id=? WHERE vendor_id IN ({marks})",
            (joseane_id, *anfarma_ids),
        ).rowcount
        movidos += conn.execute(
            f"UPDATE invoices SET vendor_id=? WHERE vendor_id IN ({marks})",
            (joseane_id, *anfarma_ids),
        ).rowcount
    like = "%ASSOCIACAO NACIONAL DE FARMACEUTICOS%"
    movidos += conn.execute(
        """
        UPDATE orders SET vendor_id=?
        WHERE vendor_id <> ?
          AND UPPER(COALESCE(vendor_nome_ref,'')) LIKE ?
        """,
        (joseane_id, joseane_id, like),
    ).rowcount
    movidos += conn.execute(
        """
        UPDATE invoices SET vendor_id=?
        WHERE vendor_id <> ?
          AND UPPER(COALESCE(vendor_nome_ref,'')) LIKE ?
        """,
        (joseane_id, joseane_id, like),
    ).rowcount
    return int(movidos or 0)


def _gravar_pedidos(conn, m, rows: list[dict], agora: str, cache: dict) -> int:
    inserts = []
    for row in rows:
        pedido = _txt(row.get("PEDIDO"))
        if not pedido:
            continue
        data_emissao = _data(row.get("DT_EMISSAO"))
        codigo = _txt(row.get("CODCLI"))
        fantasia = _txt(row.get("FANTASIA")) or _txt(row.get("CLI_NOME"))
        vendor_nome = vendedor_pedido(row)
        vendor_id = m.upsert_vendor(conn, carteira_do_responsavel(vendor_nome), cache=cache)
        qtde = _num(row.get("QTDE"))
        preco = _num(row.get("PRECO"))
        valor_item = _num(row.get("VALOR_ITEM"))
        medida = _txt(row.get("MEDIDA"))
        grupo = _txt(row.get("GRUPO"))
        tipo_item = _txt(row.get("TIPO_ITEM_PEDIDO")) or _txt(row.get("TIPO_ITEM"))
        papel = _txt(row.get("PAPEL"))
        condicao = _txt(row.get("CONDICAO_ENTREGA"))
        situacao = _txt(row.get("SITUACAO"))
        motivo = _txt(row.get("MOTIVO"))
        desc_motivo = _txt(row.get("DESC_MOTIVO"))
        cancelado = 1 if situacao.upper() == "CANCELADO" else 0
        if row.get("DT_CANC") and _data(row.get("DT_CANC")):
            cancelado = 1
        if m.texto_indica_cancelamento_py(situacao, motivo, desc_motivo):
            cancelado = 1
        is_jit = 1 if "98 - JIT" in condicao.upper() else 0
        cliente_id = m.upsert_customer(conn, fantasia, fantasia, codigo_hint=codigo, cache=cache)
        cidade = _txt(row.get("CIDADE"))
        uf = _txt(row.get("UF")).upper()
        if cliente_id and (cidade or uf):
            try:
                conn.execute(
                    """
                    UPDATE customers
                    SET cidade = CASE WHEN TRIM(COALESCE(?, '')) <> '' THEN ? ELSE cidade END,
                        uf = CASE WHEN TRIM(COALESCE(?, '')) <> '' THEN ? ELSE uf END
                    WHERE id = ?
                    """,
                    (cidade, cidade, uf, uf, int(cliente_id)),
                )
            except Exception:
                pass
        dedupe = m.build_dedupe_key(
            "PEDIDOS", medida, data_emissao or "", valor_item, pedido, codigo, vendor_nome,
            extra_refs=[_txt(row.get("CODIGO")), _txt(row.get("ORDEM"))],
        )
        entrega = _data(row.get("ENTREGA_PEDIDO"))
        produto_codigo = _txt(row.get("CODIGO"))
        produto_ref = _txt(row.get("PRODUTO_REF")) or _txt(row.get("DESCRICAO"))
        observacao = _txt(row.get("OBS"))
        inserts.append((
            None, pedido, data_emissao, cliente_id, vendor_id, valor_item, qtde, None, agora,
            medida or None, fantasia or None, vendor_nome or None, dedupe,
            condicao or None, is_jit, grupo or None, tipo_item or None, papel or None, preco, valor_item,
            situacao or None, motivo or None, desc_motivo or None, cancelado,
            entrega, produto_codigo or None, produto_ref or None, observacao or None,
            cidade or None, uf or None,
        ))
    if inserts:
        try:
            cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
            if "cidade" not in cols:
                conn.execute("ALTER TABLE orders ADD COLUMN cidade TEXT")
            if "uf" not in cols:
                conn.execute("ALTER TABLE orders ADD COLUMN uf TEXT")
        except Exception:
            pass
        conn.executemany(
            """
            INSERT INTO orders(
                import_id, pedido, data_emissao, cliente_id, vendor_id,
                valor, quantidade, raw_json, created_at,
                medida_ref, cliente_nome_ref, vendor_nome_ref, dedupe_key,
                condicao_entrega, is_jit, grupo_ref, tipo_item_ref, papel_ref, preco_ref, valor_item_ref,
                situacao_pedido, motivo_pedido, desc_motivo_pedido, pedido_cancelado,
                entrega_pedido, produto_codigo_ref, produto_ref, observacao_ref,
                cidade, uf
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            inserts,
        )
    return len(inserts)


def _lookup_vendor_pedido(conn, pedido: str, cache_pedidos: dict) -> tuple[int | None, str]:
    """Vendor_id e nome ja gravados no pedido (responsavel)."""
    pedido = (pedido or "").strip()
    if not pedido:
        return None, ""
    if pedido not in cache_pedidos:
        row = conn.execute(
            """
            SELECT vendor_id, COALESCE(vendor_nome_ref, '') AS nome
            FROM orders
            WHERE TRIM(COALESCE(pedido, '')) = TRIM(?)
            ORDER BY id ASC
            LIMIT 1
            """,
            (pedido,),
        ).fetchone()
        cache_pedidos[pedido] = (
            (int(row["vendor_id"]) if row and row["vendor_id"] else None),
            (row["nome"] or "").strip() if row else "",
        )
    return cache_pedidos[pedido]


def _gravar_faturamento(conn, m, rows: list[dict], agora: str, cache: dict) -> int:
    inserts = []
    cache_pedidos: dict = {}
    for row in rows:
        numero = _txt(row.get("FATURA"))
        if not numero:
            continue
        data_emissao = _data(row.get("DT_EMISSAO"))
        codigo = _txt(row.get("CODCLI"))
        fantasia = _txt(row.get("FANTASIA")) or _txt(row.get("CLI_NOME"))
        pedido = _txt(row.get("PEDIDO"))
        origem = vendedor_faturamento(row)
        pedido_vendor_id, pedido_vendor_nome = _lookup_vendor_pedido(conn, pedido, cache_pedidos)
        vendor_nome = pedido_vendor_nome or origem
        produto = _txt(row.get("DESCRICAO"))
        produto_codigo = _txt(row.get("CODIGO"))
        qtde = _num(row.get("QTDE"))
        preco = _num(row.get("PRECO"))
        valor = _num(row.get("VALOR_LIQ"))
        medida = _txt(row.get("MEDIDA"))
        grupo = _txt(row.get("GRUPO"))
        nat_tipo = _txt(row.get("NAT_TIPO"))
        rede_nome = _txt(row.get("REDE_NOME"))
        rede_key = _rede_key(_txt(row.get("REDE_CODIGO")))
        cliente_id = m.upsert_customer(conn, fantasia, fantasia, codigo_hint=codigo, cache=cache)
        if eh_responsavel_anfarma(origem) or eh_responsavel_anfarma(vendor_nome):
            vendor_id = m.upsert_vendor(conn, LABEL_JOSEANE, cache=cache)
            if eh_responsavel_anfarma(origem):
                vendor_nome = origem
        else:
            vendor_id = pedido_vendor_id or m.upsert_vendor(conn, vendor_nome, cache=cache)
        dedupe = m.build_dedupe_key(
            tipo="FATURAMENTO",
            medida_ref="",
            data_ref=data_emissao or "",
            valor_ref=valor,
            documento_ref=numero or pedido,
            cliente_ref=pedido,
            responsavel_ref=produto_codigo or produto,
            extra_refs=[f"PROD:{produto}", f"QTD:{qtde}", f"ORD:{row.get('ORDEM')}"],
        )
        inserts.append((
            None, numero, data_emissao, cliente_id, vendor_id, valor, qtde,
            pedido or None, produto or None, medida or None, agora,
            medida or None, fantasia or None, vendor_nome or None, dedupe,
            0, rede_nome or None, rede_key or None, grupo or None, nat_tipo or None, None,
            preco, valor, produto_codigo or None,
        ))
    if inserts:
        conn.executemany(
            """
            INSERT INTO invoices(
                import_id, numero, data_emissao, cliente_id, vendor_id,
                valor, quantidade, pedido, produto, formato,
                created_at, medida_ref, cliente_nome_ref, vendor_nome_ref, dedupe_key,
                is_jit, rede_nome, rede_key, grupo_ref, tipo_item_ref, papel_ref,
                preco_ref, valor_item_ref, produto_codigo_ref
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            inserts,
        )
    return len(inserts)


def _rotulo_status_os(codigo, descricao) -> str | None:
    """Junta codigo e descricao do STATUS_OS: '19 - PEDIDO APROVADO'."""
    rotulo = rotulo_status_os(codigo, descricao)
    return rotulo or None


def _gravar_orcamentos(conn, m, rows: list[dict], agora: str) -> int:
    inserts = []
    vistos = set()
    for row in rows:
        numero = _txt(row.get("NUMERO"))
        if not numero or numero in vistos:
            continue
        vistos.add(numero)
        data_inicio = _data(row.get("DT_INICIO"))
        valor_total = _num(row.get("VALOR_TOTAL"))
        cliente = _txt(row.get("FANTASIA")) or _txt(row.get("CLI_NOME"))
        vendedor = _txt(row.get("VEN_NOME"))
        dedupe = m.build_dedupe_key(
            tipo="ORCAMENTO",
            medida_ref=_txt(row.get("MEDIDA")),
            data_ref=data_inicio or "",
            valor_ref=valor_total,
            documento_ref=numero,
            cliente_ref=cliente,
            responsavel_ref=vendedor,
        )
        inserts.append((
            agora, numero, cliente or None, _txt(row.get("PEDIDO")) or None,
            _txt(row.get("COD_VEN")) or None, vendedor or None, data_inicio,
            _data(row.get("DT_VALIDADE")), _txt(row.get("STATUS")) or None,
            _rotulo_status_os(row.get("STATUS_OS"), row.get("DESC_STATUS")),
            _txt(row.get("TIPO_CONTATO")) or None,
            valor_total, _txt(row.get("UF")) or None,
            (_txt(row.get("DESC_MOTIVO")) or _txt(row.get("MOTIVO"))) or None,
            _txt(row.get("MEDIDA")) or None, cliente or None, vendedor or None, dedupe,
            _num(row.get("QTDE")), _num(row.get("VALOR_BASE")),
            _txt(row.get("OBSERVACAO")) or None, _txt(row.get("OBS")) or None,
            _data(row.get("DT_PREV")), _data(row.get("DT_RETORNO")),
            _txt(row.get("STATUS_OS")) or None, _txt(row.get("PROSPECCAO")) or None,
            _txt(row.get("DESC_PROSPECCAO")) or None, _txt(row.get("FUNCIONARIO")) or None,
            _txt(row.get("NOME_FUNCIONARIO")) or None, _txt(row.get("CODCLI")) or None,
            _txt(row.get("CIDADE")) or None,
        ))
    if inserts:
        conn.executemany(
            """
            INSERT INTO orcamentos(
                created_at, numero, cliente_nome, pedido_id, vendedor_id, vendedor_nome,
                data_inicio, data_validade, status, desc_status, tipo_contato,
                valor_total, uf, motivo, medida_ref, cliente_nome_ref, vendor_nome_ref, dedupe_key,
                quantidade, valor_base, observacao, obs, data_prev, data_retorno,
                status_os, prospeccao, desc_prospeccao, funcionario, nome_funcionario,
                cliente_codigo, cidade
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            inserts,
        )
    return len(inserts)


def _gravar_liquidez(conn, m, rows: list[dict], agora: str, cache: dict) -> int:
    inserts = []
    for row in rows:
        titulo = _txt(row.get("TITULO"))
        if not titulo:
            continue
        codigo = _txt(row.get("CODCLI"))
        fantasia = _txt(row.get("FANTASIA")) or _txt(row.get("CLI_NOME"))
        vendor_nome = _txt(row.get("REP_NOME"))
        pedido = _txt(row.get("PEDIDO"))
        data_pagto = _data(row.get("DT_PAGTO")) or _data(row.get("DT_STATUS")) or _data(row.get("DT_VENCTO"))
        valor_pago = _num(row.get("VALOR_PAGO"))
        valor_liq = _num(row.get("VALOR_LIQ"))
        customer_id = m.upsert_customer(conn, fantasia, fantasia, codigo_hint=codigo, cache=cache)
        vendor_id = m.upsert_vendor(conn, vendor_nome, cache=cache)
        dedupe = m.build_dedupe_key(
            tipo="LIQUIDEZ",
            medida_ref="",
            data_ref=data_pagto or "",
            valor_ref=valor_pago or valor_liq,
            documento_ref=titulo,
            cliente_ref=codigo or fantasia,
            responsavel_ref=vendor_nome,
            extra_refs=[f"PED:{pedido}", f"PARC:{row.get('PARCELA')}"],
        )
        inserts.append((
            None, vendor_id, customer_id, pedido or None, titulo, codigo or None,
            fantasia or None, valor_pago, valor_liq, data_pagto, vendor_nome or None,
            agora, None, dedupe,
        ))
    if inserts:
        conn.executemany(
            """
            INSERT INTO liquidez(
                import_id, vendor_id, customer_id, pedido, titulo, cliente_codigo,
                cliente_nome, valor_pago, valor_liq, data_pagto, responsavel,
                created_at, medida_ref, dedupe_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            inserts,
        )
    return len(inserts)


_CDC_LOTE_IN = 200


def _chaves_norm(valores) -> list[str]:
    vistos: set[str] = set()
    saida: list[str] = []
    for bruto in valores or []:
        chave = _txt(bruto)
        if not chave or chave in vistos:
            continue
        vistos.add(chave)
        saida.append(chave)
    return saida


def _delete_in(conn, tabela: str, coluna: str, chaves: list[str]) -> int:
    total = 0
    for i in range(0, len(chaves), _CDC_LOTE_IN):
        lote = chaves[i : i + _CDC_LOTE_IN]
        marcas = ",".join("?" for _ in lote)
        cur = conn.execute(
            f"DELETE FROM {tabela} WHERE TRIM(COALESCE({coluna}, '')) IN ({marcas})",
            lote,
        )
        total += int(cur.rowcount or 0)
    return total


def _select_in(sql: str, coluna: str, chaves: list[str]) -> list[dict]:
    linhas: list[dict] = []
    for i in range(0, len(chaves), _CDC_LOTE_IN):
        lote = chaves[i : i + _CDC_LOTE_IN]
        consulta = anexar_and(sql, in_trim(coluna, len(lote)))
        linhas.extend(executar_select_dicts(consulta, lote))
    return linhas


def recarregar_pedidos(conn: sqlite3.Connection, numeros) -> int:
    """Apaga e regrava so os pedidos cujos numeros mudaram no Sisplan."""
    from crm_npd import legacy_monolith as m

    chaves = _chaves_norm(numeros)
    if not chaves:
        return 0
    rows = _select_in(CRM_PEDIDOS, "P.NUMERO", chaves)
    _delete_in(conn, "orders", "pedido", chaves)
    agora = m.now_iso()
    cache = m.build_import_entity_cache(conn)
    gravadas = _gravar_pedidos(conn, m, rows, agora, cache)
    aplicar_carteira_anfarma(conn)
    logger.info("cdc.pedidos chaves=%s linhas=%s", len(chaves), gravadas)
    return gravadas


def recarregar_faturamento(conn: sqlite3.Connection, *, faturas=None, pedidos=None) -> int:
    """Apaga e regrava NFs (e DEMO/entrada) das chaves que o CDC viu."""
    from crm_npd import legacy_monolith as m

    fats = _chaves_norm(faturas)
    peds = _chaves_norm(pedidos)
    if not fats and not peds:
        return 0
    rows: list[dict] = []
    if fats:
        rows.extend(_select_in(CRM_FATURAMENTO, "N.FATURA", fats))
        rows.extend(_select_in(CRM_FATURAMENTO_ENTRADA, "NE.NOTAFISCAL", fats))
        rows.extend(_select_in(CRM_FATURAMENTO_ENTRADA, "NEI.NUMERO", fats))
        rows.extend(_select_in(CRM_FATURAMENTO_DEMO, "R.NUMERO", fats))
    if peds:
        rows.extend(_select_in(CRM_FATURAMENTO, "NI.PEDIDO", peds))
        rows.extend(_select_in(CRM_FATURAMENTO_ENTRADA, "NEI.PEDIDO", peds))
        rows.extend(_select_in(CRM_FATURAMENTO_DEMO, "R.PEDIDO", peds))
    vistos: set[tuple] = set()
    unicas: list[dict] = []
    for row in rows:
        chave = (
            _txt(row.get("FATURA")),
            _txt(row.get("CODIGO")),
            _txt(row.get("ORDEM")),
            _txt(row.get("PEDIDO")),
            _txt(row.get("VALOR_LIQ")),
        )
        if chave in vistos:
            continue
        vistos.add(chave)
        unicas.append(row)
    numeros_local = {_txt(r.get("FATURA")) for r in unicas}
    numeros_local.update(fats)
    numeros_local.update(f"E-{f}" for f in fats)
    numeros_local.update(f"DEMO-{f}" for f in fats)
    numeros_local = [n for n in numeros_local if n]
    if numeros_local:
        _delete_in(conn, "invoices", "numero", numeros_local)
    for fat in fats:
        conn.execute("DELETE FROM invoices WHERE numero LIKE ?", (f"DEMO-{fat}-%",))
    if peds:
        _delete_in(conn, "invoices", "pedido", peds)
    agora = m.now_iso()
    cache = m.build_import_entity_cache(conn)
    gravadas = _gravar_faturamento(conn, m, unicas, agora, cache)
    aplicar_carteira_anfarma(conn)
    logger.info("cdc.faturamento fats=%s peds=%s linhas=%s", len(fats), len(peds), gravadas)
    return gravadas


def recarregar_orcamentos(conn: sqlite3.Connection, numeros) -> int:
    """Apaga e regrava so os orcamentos (OS) cujos numeros mudaram."""
    from crm_npd import legacy_monolith as m

    chaves = _chaves_norm(numeros)
    if not chaves:
        return 0
    rows = _select_in(CRM_ORCAMENTOS, "O.NUMERO", chaves)
    _delete_in(conn, "orcamentos", "numero", chaves)
    agora = m.now_iso()
    gravadas = _gravar_orcamentos(conn, m, rows, agora)
    logger.info("cdc.orcamentos chaves=%s linhas=%s", len(chaves), gravadas)
    return gravadas
