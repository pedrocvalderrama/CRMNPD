"""Mede, na base real, o impacto das divergencias de regra encontradas no diagnostico.

O monolito calcula a mesma metrica de formas diferentes em lugares diferentes. Este
script quantifica cada divergencia para que a decisao de unificacao seja tomada com
numeros, e nao com descricao.

Roda sempre em modo somente leitura sobre ``tests/golden/fixture/baseline.db`` (a base
congelada do golden master), para que o resultado seja reproduzivel e nao toque na base
de trabalho.

Uso::

    .venv\\Scripts\\python.exe scripts/reconciliacao_divergencias.py
    .venv\\Scripts\\python.exe scripts/reconciliacao_divergencias.py --ciclo 2026-09
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

FIXTURE_DB = PROJECT_ROOT / "tests" / "golden" / "fixture" / "baseline.db"

#: Mesmo valor usado por tests/golden/freeze.py, para que os recortes coincidam.
DATA_REFERENCIA = date(2026, 10, 1)

from crm_npd.domain import EstatisticasIdentidade  # noqa: E402
from crm_npd.metrics import comissao as f_comissao  # noqa: E402
from crm_npd.metrics import periodos as f_periodos  # noqa: E402
from crm_npd.metrics import score_cliente as f_score_cliente  # noqa: E402

LARGURA = 78


def abrir() -> sqlite3.Connection:
    """Abre a base congelada em modo somente leitura.

    Returns:
        Conexao sqlite3 com ``row_factory`` configurada.

    Raises:
        SystemExit: Se a base congelada nao existir.
    """
    if not FIXTURE_DB.exists():
        raise SystemExit(
            f"Base congelada nao encontrada em {FIXTURE_DB}.\n"
            "Rode antes: .venv\\Scripts\\python.exe scripts/golden_capture.py"
        )
    conn = sqlite3.connect(f"file:{FIXTURE_DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def titulo(numero: int, texto: str) -> None:
    """Imprime o cabecalho de uma divergencia."""
    print()
    print("=" * LARGURA)
    print(f"DIVERGENCIA {numero} - {texto}")
    print("=" * LARGURA)


def linha(rotulo: str, *valores: Any) -> None:
    """Imprime um par rotulo/valores alinhado."""
    print(f"  {rotulo:<44}" + "  ".join(str(v) for v in valores))


def brl(valor: float) -> str:
    """Formata um numero como moeda brasileira."""
    inteiro, _, centavos = f"{float(valor):,.2f}".partition(".")
    return "R$ " + inteiro.replace(",", ".") + "," + centavos


# ----------------------------------------------------------------------------- 1


def primeiro_faturamento_por_identidade(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Devolve o primeiro faturamento historico de cada identidade.

    Replica a ordenacao do monolito (``data_emissao ASC, id ASC``) para que a
    comparacao seja justa.
    """
    primeiros: dict[str, sqlite3.Row] = {}
    for r in conn.execute("""
        SELECT i.id, i.numero, i.pedido, i.data_emissao,
               COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || i.cliente_id) AS ik
        FROM invoices i
        LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
        WHERE i.data_emissao IS NOT NULL AND TRIM(i.data_emissao) <> ''
        ORDER BY date(i.data_emissao) ASC, i.id ASC
    """):
        primeiros.setdefault(r["ik"], r)
    return primeiros


def div1_cliente_novo(conn: sqlite3.Connection, d0: date, d1: date) -> None:
    """Compara os dois critérios de 'cliente NOVO' e mede o impacto em comissao."""
    titulo(1, "Critério de cliente NOVO: data+documento vs apenas data")
    print("  A (data + documento) .. compute_vendor_commission, mix de performance")
    print("  B (apenas data) ....... summarize_*_by_orders, mix por identidade")
    print(f"  Ciclo analisado: {d0} a {d1}")

    primeiros = primeiro_faturamento_por_identidade(conn)

    rows = conn.execute("""
        SELECT i.id, i.numero, i.pedido, i.data_emissao, COALESCE(i.valor,0) AS valor,
               COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || i.cliente_id) AS ik
        FROM invoices i
        LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
        WHERE date(i.data_emissao) BETWEEN date(?) AND date(?)
    """, (d0.isoformat(), d1.isoformat())).fetchall()

    novos_a: set[str] = set()
    novos_b: set[str] = set()
    linhas_novas_a = 0
    linhas_novas_b = 0
    valor_novo_a = 0.0
    valor_novo_b = 0.0
    #: Identidades que, pelo critério A, caem em novo_ids E em rep_ids no mesmo ciclo.
    recorrentes_a: set[str] = set()

    for r in rows:
        ik = r["ik"]
        primeiro = primeiros.get(ik)
        if not primeiro:
            continue
        primeira_data = (primeiro["data_emissao"] or "").strip()
        dentro = bool(primeira_data and d0.isoformat() <= primeira_data <= d1.isoformat())
        if not dentro:
            continue

        doc_atual = str(r["numero"] or r["pedido"] or f"ID:{r['id']}").strip()
        doc_primeiro = str(primeiro["numero"] or primeiro["pedido"] or f"ID:{primeiro['id']}").strip()

        novos_b.add(ik)
        linhas_novas_b += 1
        valor_novo_b += float(r["valor"] or 0)

        if doc_atual == doc_primeiro:
            novos_a.add(ik)
            linhas_novas_a += 1
            valor_novo_a += float(r["valor"] or 0)
        else:
            recorrentes_a.add(ik)

    linha("Linhas de NF marcadas NOVO (A)", linhas_novas_a)
    linha("Linhas de NF marcadas NOVO (B)", linhas_novas_b)
    linha("Identidades NOVO (A)", len(novos_a))
    linha("Identidades NOVO (B)", len(novos_b))
    linha("Base faturada marcada NOVO (A)", brl(valor_novo_a))
    linha("Base faturada marcada NOVO (B)", brl(valor_novo_b))

    bonus_a = valor_novo_a * (f_comissao.PCT_BONUS_CLIENTE_NOVO / 100.0)
    bonus_b = valor_novo_b * (f_comissao.PCT_BONUS_CLIENTE_NOVO / 100.0)
    linha("Bonus de 1% pago com A", brl(bonus_a))
    linha("Bonus de 1% pago com B", brl(bonus_b))
    print()
    print(f"  >> IMPACTO EM DINHEIRO NO CICLO: {brl(bonus_b - bonus_a)} a mais com o critério B.")
    print(f"  >> Anualizado (12 ciclos): {brl((bonus_b - bonus_a) * 12)}")
    dupla = novos_a & recorrentes_a
    print()
    linha("Identidades contadas como NOVO e recorrente", len(dupla))
    print("  >> Em performance.py, novo_ids e rep_ids sao conjuntos independentes. Essas")
    print(f"     {len(dupla)} identidades entram nos DOIS, entao novo_count + rep_count")
    print(f"     soma {len(novos_a) + len(recorrentes_a)} onde existem apenas {len(novos_b)} clientes novos de fato.")
    print("     Isso infla a dimensao 'carteira' (10% do score do vendedor).")


# ----------------------------------------------------------------------------- 2


def div2_unidade_contagem(conn: sqlite3.Connection, d0: date, d1: date) -> None:
    """Compara contagem de mix por pedido versus por identidade."""
    titulo(2, "Unidade de contagem do mix Novo/Recorrente: pedido vs identidade")
    print("  A (por pedido unico) ..... summarize_team_status_by_orders")
    print("  B (por identidade) ....... mix exibido na Visao do Vendedor")

    rows = conn.execute("""
        SELECT COALESCE(NULLIF(TRIM(i.pedido),''), 'NF:' || i.id) AS pedido_ref,
               COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || i.cliente_id) AS ik
        FROM invoices i
        LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
        WHERE date(i.data_emissao) BETWEEN date(?) AND date(?)
    """, (d0.isoformat(), d1.isoformat())).fetchall()

    pedidos = {r["pedido_ref"] for r in rows}
    identidades = {r["ik"] for r in rows}
    por_identidade: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        por_identidade[r["ik"]].add(r["pedido_ref"])

    linha("Pedidos unicos no ciclo (denominador A)", len(pedidos))
    linha("Identidades no ciclo (denominador B)", len(identidades))
    if identidades:
        linha("Pedidos por identidade (media)", f"{len(pedidos) / len(identidades):.2f}")
    maiores = sorted(por_identidade.items(), key=lambda kv: len(kv[1]), reverse=True)[:3]
    for ik, peds in maiores:
        linha(f"  {ik[:40]}", f"{len(peds)} pedidos")
    print()
    print("  >> A mesma tela mostra 'X novos' em duas unidades diferentes. Um cliente novo")
    print("     com 4 pedidos no ciclo conta 4 em A e 1 em B.")


# ----------------------------------------------------------------------------- 3 e 4


def datas_por_identidade(conn: sqlite3.Connection) -> tuple[dict[str, list[date]], dict[str, list[date]]]:
    """Devolve as datas de faturamento por identidade, em duas granularidades.

    Returns:
        Tupla ``(por_linha, por_dia)``. ``por_linha`` mantem uma entrada por linha de
        nota, como faz o pipeline de score; ``por_dia`` agrupa por dia unico, como faz
        o painel de recompra.
    """
    por_linha: dict[str, list[date]] = defaultdict(list)
    por_dia: dict[str, set[date]] = defaultdict(set)
    for r in conn.execute("""
        SELECT COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || i.cliente_id) AS ik,
               substr(TRIM(i.data_emissao),1,10) AS d
        FROM invoices i
        LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
        WHERE COALESCE(i.valor,0) > 0 AND COALESCE(i.data_emissao,'') <> ''
        ORDER BY i.data_emissao ASC
    """):
        try:
            d = date.fromisoformat(r["d"])
        except (TypeError, ValueError):
            continue
        por_linha[r["ik"]].append(d)
        por_dia[r["ik"]].add(d)
    return por_linha, {k: sorted(v) for k, v in por_dia.items()}


def _intervalos(datas: Iterable[date]) -> list[int]:
    """Calcula os intervalos em dias entre datas consecutivas."""
    seq = list(datas)
    return [(seq[i] - seq[i - 1]).days for i in range(1, len(seq)) if (seq[i] - seq[i - 1]).days >= 0]


def div3_mediana_vs_media(conn: sqlite3.Connection) -> None:
    """Compara mediana e media do intervalo de recompra e o efeito no flag de atraso."""
    titulo(3, "Intervalo de recompra: mediana vs média")
    print("  A (mediana) .. calcular_metricas_recompra, score do cliente, RECUPERADO")
    print("  B (media) .... _build_repurchase_map, coluna 'atrasado' da lista de clientes")

    _, por_dia = datas_por_identidade(conn)
    ref = DATA_REFERENCIA

    divergem_atraso = 0
    comparaveis = 0
    difs: list[float] = []
    so_a = 0
    so_b = 0
    for datas in por_dia.values():
        intervalos = _intervalos(datas)
        if not intervalos:
            continue
        comparaveis += 1
        mediana = float(statistics.median(intervalos))
        media = round(sum(intervalos) / len(intervalos), 1)
        difs.append(media - mediana)
        atraso = (ref - datas[-1]).days
        atrasado_a = mediana > 0 and atraso > mediana
        atrasado_b = atraso - media > 0
        if atrasado_a != atrasado_b:
            divergem_atraso += 1
            if atrasado_a:
                so_a += 1
            else:
                so_b += 1

    linha("Identidades com 2+ compras", comparaveis)
    linha("Identidades onde o flag 'atrasado' divergeu", divergem_atraso)
    linha("  atrasado só pela mediana", so_a)
    linha("  atrasado só pela média", so_b)
    if difs:
        linha("Média - mediana (mediana das diferencas)", f"{statistics.median(difs):+.1f} dias")
        linha("Média - mediana (maior diferenca)", f"{max(difs):+.1f} dias")
    if comparaveis:
        pct = divergem_atraso / comparaveis * 100.0
        print()
        print(f"  >> {pct:.1f}% dos clientes com histórico recebem classificacao de atraso")
        print("     diferente dependendo da tela aberta.")


def div4_unidade_compras(conn: sqlite3.Connection) -> None:
    """Compara contagem de compras por linha de nota versus por dia unico."""
    titulo(4, "Contagem de compras no score do cliente: linha de NF vs dia único")
    print("  A (linha de NF) ... _build_score_map: pedidos += 1 por linha")
    print("  B (dia único) ..... _build_repurchase_map: qtd = len(dias)")
    print("  A granularidade entra em DUAS pontas do score: no numero de compras e nos")
    print("  intervalos (linhas no mesmo dia geram intervalo zero e derrubam a mediana).")

    por_linha, por_dia = datas_por_identidade(conn)
    valor_max = 0.0
    totais: dict[str, float] = {}
    for r in conn.execute("""
        SELECT COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || i.cliente_id) AS ik,
               COALESCE(SUM(COALESCE(i.valor,0)),0) AS total
        FROM invoices i
        LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
        WHERE COALESCE(i.valor,0) > 0 AND COALESCE(i.data_emissao,'') <> ''
        GROUP BY 1
    """):
        totais[r["ik"]] = float(r["total"] or 0)
        valor_max = max(valor_max, float(r["total"] or 0))

    def score(ik: str, datas: list[date], qtd: int) -> tuple[float, str]:
        """Calcula score e faixa para uma identidade com a granularidade dada."""
        intervalos = _intervalos(datas)
        mediano = float(statistics.median(intervalos)) if intervalos else None
        ultima = datas[-1] if datas else None
        atraso = (DATA_REFERENCIA - ultima).days if ultima else None
        stats = EstatisticasIdentidade(
            valor_total=totais.get(ik, 0.0),
            pedidos=qtd,
            intervalo_mediano=mediano,
            atraso_atual=atraso,
            ultima_data=ultima,
        )
        resultado = f_score_cliente.calcular_score(stats, valor_max)
        return resultado.score, str(resultado.faixa)

    mudou_faixa = 0
    mudou_score = 0
    total_ident = 0
    piores: list[tuple[float, str, float, float]] = []
    for ik, datas_linha in por_linha.items():
        datas_dia = por_dia.get(ik, [])
        if not datas_dia:
            continue
        total_ident += 1
        s_a, f_a = score(ik, datas_linha, len(datas_linha))
        s_b, f_b = score(ik, datas_dia, len(datas_dia))
        if abs(s_a - s_b) > 1e-9:
            mudou_score += 1
            piores.append((abs(s_a - s_b), ik, s_a, s_b))
        if f_a != f_b:
            mudou_faixa += 1

    linha("Identidades avaliadas", total_ident)
    linha("Score diferente entre A e B", mudou_score)
    linha("FAIXA diferente entre A e B", mudou_faixa)
    for dif, ik, s_a, s_b in sorted(piores, reverse=True)[:5]:
        linha(f"  {ik[:38]}", f"A={s_a:.1f}  B={s_b:.1f}  (dif {dif:.1f})")
    if total_ident:
        print()
        print(f"  >> {mudou_faixa / total_ident * 100:.1f}% das identidades trocariam de faixa")
        print("     (A+/A/B/C/D) só por mudar a unidade de contagem.")


# ----------------------------------------------------------------------------- 5


def div5_conversao(conn: sqlite3.Connection, ano: int, ref: date) -> None:
    """Compara as implementacoes de taxa de conversao no mesmo recorte."""
    titulo(5, "Taxa de conversão: quatro fórmulas com o mesmo nome")
    ini = date(ano, 1, 1).isoformat()
    fim = ref.isoformat()
    print(f"  Recorte: {ini} a {fim}")

    a = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN TRIM(COALESCE(pedido_id,'')) <> ''
                         OR UPPER(TRIM(COALESCE(desc_status,''))) = 'PEDIDO APROVADO'
                        THEN 1 ELSE 0 END) AS fechados
        FROM orcamentos WHERE date(data_inicio) BETWEEN date(?) AND date(?)
    """, (ini, fim)).fetchone()

    c = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN TRIM(COALESCE(pedido_id,'')) NOT IN ('','0','None')
                        THEN 1 ELSE 0 END) AS fechados
        FROM orcamentos WHERE date(data_inicio) BETWEEN date(?) AND date(?)
    """, (ini, fim)).fetchone()

    from crm_npd import legacy_monolith as legacy

    orc = conn.execute("""
        SELECT desc_status, pedido_id, status
        FROM orcamentos WHERE date(data_inicio) BETWEEN date(?) AND date(?)
    """, (ini, fim)).fetchall()
    ganhos = sum(1 for r in orc if legacy._sisplan_stage(r["desc_status"], r["pedido_id"], r["status"]) == "GANHO")

    ped = conn.execute(
        "SELECT COUNT(*) AS q FROM orders WHERE date(data_emissao) BETWEEN date(?) AND date(?)",
        (ini, fim),
    ).fetchone()
    fat = conn.execute(
        "SELECT COUNT(*) AS q FROM invoices WHERE date(data_emissao) BETWEEN date(?) AND date(?)",
        (ini, fim),
    ).fetchone()

    total = int(a["total"] or 0)
    pct = lambda n, d: (n / d * 100.0) if d else 0.0  # noqa: E731

    linha("A Estudo de Mercado (pedido_id ou APROVADO)", f"{pct(int(a['fechados'] or 0), total):.2f}%",
          f"({int(a['fechados'] or 0)}/{total})")
    linha("B Performance (_sisplan_stage == GANHO)", f"{pct(ganhos, total):.2f}%", f"({ganhos}/{total})")
    linha("C metrics_orc (pedido_id nao vazio)", f"{pct(int(c['fechados'] or 0), total):.2f}%",
          f"({int(c['fechados'] or 0)}/{total})")
    linha("D Operacao (NFs / pedidos)", f"{pct(int(fat['q'] or 0), int(ped['q'] or 0)):.2f}%",
          f"({int(fat['q'] or 0)}/{int(ped['q'] or 0)})")
    print()
    print("  >> D nao é conversão de proposta: é relação pedido->faturamento. O rótulo")
    print("     'CONVERSÃO' na tela de operação mede outra coisa.")


# ----------------------------------------------------------------------------- 6


def div6_churn(conn: sqlite3.Connection, ano: int, ref: date) -> None:
    """Compara a chave de agrupamento do churn com a identidade comercial do CRM."""
    titulo(6, "Chave de agrupamento do churn: rede_key vs identity_key")
    ly0, ly1 = f"{ano - 1}-01-01", f"{ano - 1}-12-31"
    cy0, cy1 = f"{ano}-01-01", ref.isoformat()
    print(f"  Perderam: comprou em {ano - 1} e nao voltou de {cy0} a {cy1}")

    atual = conn.execute("""
        WITH ly AS (
            SELECT DISTINCT COALESCE(NULLIF(TRIM(rede_key),''), 'CID:' || COALESCE(cliente_id,0),
                   'N:' || UPPER(TRIM(COALESCE(cliente_nome_ref,'')))) AS chave
            FROM invoices WHERE date(data_emissao) BETWEEN date(?) AND date(?)
        ), cy AS (
            SELECT DISTINCT COALESCE(NULLIF(TRIM(rede_key),''), 'CID:' || COALESCE(cliente_id,0),
                   'N:' || UPPER(TRIM(COALESCE(cliente_nome_ref,'')))) AS chave
            FROM invoices WHERE date(data_emissao) BETWEEN date(?) AND date(?)
        )
        SELECT COUNT(*) AS qtd FROM ly LEFT JOIN cy ON cy.chave = ly.chave WHERE cy.chave IS NULL
    """, (ly0, ly1, cy0, cy1)).fetchone()

    proposto = conn.execute("""
        WITH ly AS (
            SELECT DISTINCT COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || i.cliente_id) AS chave
            FROM invoices i LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
            WHERE date(i.data_emissao) BETWEEN date(?) AND date(?)
        ), cy AS (
            SELECT DISTINCT COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || i.cliente_id) AS chave
            FROM invoices i LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
            WHERE date(i.data_emissao) BETWEEN date(?) AND date(?)
        )
        SELECT COUNT(*) AS qtd FROM ly LEFT JOIN cy ON cy.chave = ly.chave WHERE cy.chave IS NULL
    """, (ly0, ly1, cy0, cy1)).fetchone()

    cobertura = conn.execute("""
        SELECT SUM(CASE WHEN TRIM(COALESCE(rede_key,'')) <> '' THEN 1 ELSE 0 END) AS com_rede,
               COUNT(*) AS total
        FROM invoices WHERE date(data_emissao) BETWEEN date(?) AND date(?)
    """, (ly0, cy1)).fetchone()

    grupos = conn.execute(
        "SELECT COUNT(*) AS q FROM customer_identity_rules WHERE identity_key LIKE 'GRUPO:%'"
    ).fetchone()

    linha("Churn com rede_key (implementacao atual)", int(atual["qtd"] or 0))
    linha("Churn com identity_key (resto do CRM)", int(proposto["qtd"] or 0))
    linha("NFs com rede_key preenchida", f"{int(cobertura['com_rede'] or 0)} de {int(cobertura['total'] or 0)}")
    linha("Identidades no nivel GRUPO", int(grupos["q"] or 0))
    dif = int(atual["qtd"] or 0) - int(proposto["qtd"] or 0)
    print()
    print(f"  >> Diferenca de {abs(dif)} cliente(s) no numero de churn exibido no Estudo")
    print("     de Mercado. rede_key ignora o nivel GRUPO, entao um grupo com N redes")
    print("     aparece como N perdas em vez de uma.")


# -----------------------------------------------------------------------------


def main() -> int:
    """Executa todas as medicoes e imprime o relatorio."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ciclo", default="", help="Mes do ciclo de faturamento, ex. 2026-09")
    args = parser.parse_args()

    if args.ciclo:
        ano, mes = (int(x) for x in args.ciclo.split("-"))
        periodo = f_periodos.ciclo_faturamento(date(ano, mes, 1))
    else:
        periodo = f_periodos.ciclo_faturamento_ate_hoje(DATA_REFERENCIA, DATA_REFERENCIA)
    d0, d1 = periodo.inicio, periodo.fim

    print()
    print("#" * LARGURA)
    print("# RECONCILIACAO DE DIVERGENCIAS DE REGRA DE NEGOCIO")
    print(f"# Base: {FIXTURE_DB.name} (somente leitura)   Data de referencia: {DATA_REFERENCIA}")
    print("#" * LARGURA)

    conn = abrir()
    try:
        div1_cliente_novo(conn, d0, d1)
        div2_unidade_contagem(conn, d0, d1)
        div3_mediana_vs_media(conn)
        div4_unidade_compras(conn)
        div5_conversao(conn, DATA_REFERENCIA.year, DATA_REFERENCIA)
        div6_churn(conn, DATA_REFERENCIA.year, DATA_REFERENCIA)
    finally:
        conn.close()

    print()
    print("=" * LARGURA)
    print("Nenhuma unificacao foi aplicada. Este script apenas mede.")
    print("=" * LARGURA)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
