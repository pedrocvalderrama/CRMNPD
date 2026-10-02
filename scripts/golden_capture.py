"""Captura golden-master do CRM NPD.

Grava em JSON o resultado de todo calculo de negocio do sistema atual, para que
qualquer refatoracao possa ser comparada numero por numero contra o
comportamento original.

Uso:
    .venv\\Scripts\\python.exe scripts\\golden_capture.py            # grava em output/
    .venv\\Scripts\\python.exe scripts\\golden_capture.py --baseline # grava em baseline/

O baseline e versionado no git e so deve ser regravado quando uma mudanca de
numero for intencional e aprovada.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

GOLDEN_DIR = PROJECT_ROOT / "tests" / "golden"
FIXTURE_DB = GOLDEN_DIR / "fixture" / "baseline.db"
# Copia de trabalho descartavel. A subida do app aplica migracoes e grava no
# banco, entao a fixture nunca pode ser aberta diretamente: ela precisa manter o
# mesmo sha256 para que a comparacao entre execucoes signifique algo.
WORK_DB = GOLDEN_DIR / "fixture" / "work.db"
BANCO_ORIGEM = PROJECT_ROOT / "crm_npd.db"

# Chaves cujo valor e grafico, HTML ou blob: nao entram na comparacao numerica.
CHAVES_IGNORADAS = re.compile(
    r"(chart|grafico|img|image|png|base64|html|svg|_b64)", re.IGNORECASE
)


def congelar_banco() -> Path:
    """Copia o banco de producao para uma fixture imutavel, se ainda nao existir.

    A captura nunca roda sobre ``crm_npd.db``: a subida do app aplica migracoes e
    o uso normal do CRM grava tarefas, o que faria o baseline derivar.

    Returns:
        Caminho da fixture congelada.

    Raises:
        FileNotFoundError: Se o banco de origem nao existir.
    """
    if not FIXTURE_DB.exists():
        if not BANCO_ORIGEM.exists():
            raise FileNotFoundError(
                f"Banco de origem nao encontrado: {BANCO_ORIGEM}. "
                "A captura golden-master precisa da base real."
            )
        FIXTURE_DB.parent.mkdir(parents=True, exist_ok=True)
        print(f"Congelando {BANCO_ORIGEM.name} -> {FIXTURE_DB.relative_to(PROJECT_ROOT)}")
        shutil.copy2(BANCO_ORIGEM, FIXTURE_DB)
    return FIXTURE_DB


def preparar_copia_trabalho() -> Path:
    """Cria a copia descartavel sobre a qual a aplicacao vai rodar.

    Returns:
        Caminho da copia de trabalho, recem-criada a partir da fixture.
    """
    for sufixo in ("", "-wal", "-shm"):
        resto = Path(str(WORK_DB) + sufixo)
        resto.unlink(missing_ok=True)
    shutil.copy2(FIXTURE_DB, WORK_DB)
    return WORK_DB


def normalizar(valor: Any, _profundidade: int = 0) -> Any:
    """Converte um resultado qualquer em algo estavel e comparavel em JSON.

    Arredonda floats para eliminar ruido de ponto flutuante, ordena dicionarios,
    resume strings longas por digest e descarta graficos.

    Args:
        valor: Valor retornado por uma funcao do monolito.
        _profundidade: Controle interno de recursao.

    Returns:
        Estrutura serializavel em JSON de forma deterministica.
    """
    if _profundidade > 12:
        return "<profundidade-maxima>"
    if valor is None or isinstance(valor, bool | int | str):
        if isinstance(valor, str) and len(valor) > 300:
            return f"<str:{len(valor)}:{hashlib.sha256(valor.encode()).hexdigest()[:16]}>"
        return valor
    if isinstance(valor, float):
        # 6 casas absorvem ruido binario e ainda detectam qualquer mudanca real
        # de centavo, score ou percentual.
        return round(valor, 6)
    if isinstance(valor, date):
        return valor.isoformat()
    if isinstance(valor, dict):
        saida: dict[str, Any] = {}
        for chave in sorted(valor, key=str):
            nome = str(chave)
            if CHAVES_IGNORADAS.search(nome):
                continue
            saida[nome] = normalizar(valor[chave], _profundidade + 1)
        return saida
    if isinstance(valor, list | tuple | set):
        itens = list(valor)
        if isinstance(valor, set):
            itens = sorted(itens, key=str)
        return [normalizar(item, _profundidade + 1) for item in itens]
    # sqlite3.Row e objetos com keys()
    if hasattr(valor, "keys"):
        try:
            return normalizar(dict(valor), _profundidade + 1)
        except Exception:
            pass
    return f"<{type(valor).__name__}>"


def seguro(rotulo: str, funcao: Any, *args: Any, **kwargs: Any) -> Any:
    """Executa um calculo capturando falhas como valor.

    Um erro tambem e comportamento observavel: se a refatoracao passar a lancar
    excecao onde antes retornava numero, o golden master precisa acusar.

    Args:
        rotulo: Identificacao usada na mensagem de erro.
        funcao: Callable a executar.
        *args: Argumentos posicionais.
        **kwargs: Argumentos nomeados.

    Returns:
        Resultado normalizado, ou dicionario de erro.
    """
    try:
        return normalizar(funcao(*args, **kwargs))
    except Exception as exc:  # noqa: BLE001 - erro e dado de regressao
        return {"__erro__": type(exc).__name__, "mensagem": str(exc)[:300], "onde": rotulo}


def _meses_referencia() -> list[date]:
    """Datas de referencia mensais que cobrem o periodo com dados reais."""
    meses = [date(2026, m, 15) for m in range(1, 11)]
    meses += [date(2025, m, 15) for m in (3, 6, 9, 12)]
    return meses


def capturar(legacy: Any, hoje: date) -> dict[str, Any]:
    """Executa a captura completa sobre a base congelada.

    Args:
        legacy: Modulo ``crm_npd.legacy_monolith`` ja com os patches aplicados.
        hoje: Data congelada usada como referencia.

    Returns:
        Dicionario com todas as secoes capturadas.
    """
    dados: dict[str, Any] = {
        "__meta__": {
            "data_referencia": hoje.isoformat(),
            "fixture_sha256": _sha256_arquivo(FIXTURE_DB),
            "versao_captura": 1,
        }
    }

    with legacy.conectar() as conn:
        dados["periodos"] = _capturar_periodos(legacy)
        dados["faturamento_mensal"] = _capturar_faturamento(legacy, conn)
        dados["metas"] = _capturar_metas(legacy, conn)
        vendors = _listar_vendors(conn)
        dados["vendedores"] = _capturar_vendedores(legacy, conn, vendors)
        dados["mix_time"] = _capturar_mix_time(legacy, conn)
        dados["score_identidades"] = _capturar_scores(legacy, conn)
        dados["clientes"] = _capturar_clientes(legacy, conn)
        dados["funil"] = _capturar_funil(legacy, conn)
        dados["orcamentos"] = _capturar_orcamentos(legacy, conn)
        dados["performance"] = _capturar_performance(legacy, conn)
        dados["mercado"] = seguro("market", legacy._market_snapshot, conn, hoje)

    return dados


def _sha256_arquivo(caminho: Path) -> str:
    digest = hashlib.sha256()
    with caminho.open("rb") as fh:
        for bloco in iter(lambda: fh.read(1 << 20), b""):
            digest.update(bloco)
    return digest.hexdigest()


def _capturar_periodos(legacy: Any) -> dict[str, Any]:
    """Funcoes de periodo: ciclo de faturamento, mes civil e dias uteis."""
    saida: dict[str, Any] = {}
    for ref in _meses_referencia():
        chave = ref.strftime("%Y-%m")
        saida[chave] = {
            "ultimo_periodo_fechado": seguro("p1", legacy.ultimo_periodo_fechado, ref),
            "em_andamento": seguro("p2", legacy.periodo_em_andamento_25_ate_hoje, ref),
            "ciclo_que_contem": seguro("p3", legacy.periodo_ciclo_25_24_que_contem_hoje, ref),
            "mes_calendario": seguro("p4", legacy.periodo_mes_calendario, ref),
        }
    inicio, fim = legacy.periodo_mes_calendario(date(2026, 9, 15))
    saida["dias_uteis_setembro_2026"] = seguro("du", legacy.business_days_between, inicio, fim)
    return saida


def _capturar_faturamento(legacy: Any, conn: Any) -> dict[str, Any]:
    saida: dict[str, Any] = {}
    for ref in _meses_referencia():
        saida[ref.strftime("%Y-%m")] = seguro(
            "fat", legacy.faturamento_mes_calendario, conn, ref, True
        )
    return saida


def _capturar_metas(legacy: Any, conn: Any) -> dict[str, Any]:
    return {
        "meta_empresa": seguro("meta_emp", legacy.meta_empresa, conn),
        "meta_empresa_calc_3m": seguro("meta_calc", legacy.meta_empresa_calc, conn, 3, 1.0),
    }


def _listar_vendors(conn: Any) -> list[dict[str, Any]]:
    """Vendedores com faturamento, ordenados por id para captura deterministica."""
    rows = conn.execute(
        """
        SELECT v.id, v.nome, v.codigo, v.tipo
        FROM vendors v
        WHERE EXISTS (SELECT 1 FROM invoices i WHERE i.vendor_id = v.id)
        ORDER BY v.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _capturar_vendedores(legacy: Any, conn: Any, vendors: list[dict[str, Any]]) -> dict[str, Any]:
    """Comissao, meta e mix por vendedor nos ultimos ciclos fechados."""
    ciclos = [date(2026, m, 15) for m in (6, 7, 8, 9, 10)]
    saida: dict[str, Any] = {}
    for v in vendors:
        vid = int(v["id"])
        bloco: dict[str, Any] = {
            "nome": v["nome"],
            "codigo": v["codigo"],
            "tipo": v["tipo"],
            "meta": seguro("meta_v", legacy.meta_vendedor, conn, vid),
            "recompra": seguro("recompra_v", legacy.vendor_repurchase_metrics, conn, vid),
            "ciclos": {},
        }
        for ref in ciclos:
            d0, d1 = legacy.ultimo_periodo_fechado(ref)
            bloco["ciclos"][ref.strftime("%Y-%m")] = {
                "comissao": seguro("com", legacy.compute_vendor_commission, conn, vid, d0, d1),
                "liquidez_externa": seguro(
                    "liq", legacy.realizado_externo_liquidez, conn, vid, d0, d1
                ),
                "mix": seguro(
                    "mix", legacy.summarize_vendor_entities_in_period, conn, vid, d0, d1
                ),
            }
        saida[str(vid)] = bloco
    return saida


def _capturar_mix_time(legacy: Any, conn: Any) -> dict[str, Any]:
    """Mix Novo/Repeticao da equipe: a versao usada no donut do dashboard."""
    saida: dict[str, Any] = {}
    for ref in [date(2026, m, 15) for m in (7, 8, 9, 10)]:
        d0, d1 = legacy.periodo_mes_calendario(ref)
        chave = ref.strftime("%Y-%m")
        saida[chave] = {
            "geral": seguro("mt", legacy.summarize_team_entities_in_period, conn, d0, d1, None),
            "interno": seguro(
                "mt_i", legacy.summarize_team_entities_in_period, conn, d0, d1, "Interno"
            ),
            "externo": seguro(
                "mt_e", legacy.summarize_team_entities_in_period, conn, d0, d1, "Externo"
            ),
        }
    return saida


def _capturar_scores(legacy: Any, conn: Any) -> dict[str, Any]:
    """Score de todas as identidades, com resumo agregado e amostra ordenada.

    O mapa completo tem milhares de entradas. Guardamos o agregado (que detecta
    qualquer deslocamento de distribuicao) e as 60 primeiras identidades em ordem
    alfabetica de chave (que permite diff legivel).
    """
    mapa = legacy.build_identity_score_map(conn, user=None, customer_ids_scope=None)
    if not isinstance(mapa, dict):
        return {"__erro__": "mapa_invalido"}

    por_faixa: dict[str, int] = {}
    soma = 0.0
    for info in mapa.values():
        faixa = str((info or {}).get("faixa", "?"))
        por_faixa[faixa] = por_faixa.get(faixa, 0) + 1
        soma += float((info or {}).get("score", 0) or 0)

    chaves = sorted(mapa)
    amostra = {k: normalizar(mapa[k]) for k in chaves[:60]}
    return {
        "total_identidades": len(mapa),
        "soma_scores": round(soma, 6),
        "distribuicao_faixas": dict(sorted(por_faixa.items())),
        "amostra_60_primeiras": amostra,
        "prioridades": seguro(
            "prio",
            lambda: {k: legacy.get_task_priority_from_score(mapa[k]) for k in chaves[:20]},
        ),
    }


def _capturar_clientes(legacy: Any, conn: Any) -> dict[str, Any]:
    """Metricas por cliente para uma amostra deterministica."""
    rows = conn.execute(
        """
        SELECT i.cliente_id, COUNT(*) AS n
        FROM invoices i
        WHERE i.cliente_id IS NOT NULL
        GROUP BY i.cliente_id
        HAVING n >= 3
        ORDER BY n DESC, i.cliente_id ASC
        LIMIT 30
        """
    ).fetchall()
    saida: dict[str, Any] = {}
    for r in rows:
        cid = int(r["cliente_id"])
        identity = seguro("ident", legacy.get_customer_identity_key, conn, cid)
        saida[str(cid)] = {
            "notas": int(r["n"]),
            "identity_key": identity,
            "metrics": seguro("cm", legacy.cliente_metrics, conn, cid),
            "recompra_tarefa": seguro(
                "crt", legacy.calcular_metricas_recompra_tarefa, conn, cid, ""
            ),
            "recompra_identidade": seguro(
                "sir",
                legacy.summarize_identity_repurchase,
                conn,
                identity if isinstance(identity, str) else "",
                date(2026, 10, 1),
            ),
        }
    return saida


def _capturar_funil(legacy: Any, conn: Any) -> dict[str, Any]:
    """Etapas, probabilidades e metricas de pipeline do 104."""
    saida: dict[str, Any] = {
        "estagios": {
            f"{desc}|{pedido}|{codigo}": seguro(
                "stage", legacy._sisplan_stage, desc, pedido, codigo
            )
            for desc, pedido, codigo in [
                ("PEDIDO APROVADO", None, None),
                ("PERDIDO", None, None),
                ("EM NEGOCIACAO", None, None),
                ("AGUARDANDO APROVACAO", None, None),
                ("PRIMEIRO CONTATO", None, None),
                ("QUALQUER COISA", "12345", None),
                ("QUALQUER COISA", None, "F"),
                ("", None, None),
                (None, None, None),
            ]
        },
        "probabilidades": normalizar(getattr(legacy, "SISPLAN_STAGE_PROB", {})),
        "periodos": {},
    }
    for ref in [date(2026, m, 15) for m in (7, 8, 9, 10)]:
        d0, d1 = legacy.periodo_mes_calendario(ref)
        rows = legacy._sisplan_period_rows(conn, d0, d1, None, "", "")
        saida["periodos"][ref.strftime("%Y-%m")] = {
            "linhas": len(rows) if rows is not None else None,
            "metricas": seguro("fm", legacy._sisplan_metrics_from_rows, rows),
        }
    return saida


def _capturar_orcamentos(legacy: Any, conn: Any) -> dict[str, Any]:
    saida: dict[str, Any] = {}
    for ref in [date(2026, m, 15) for m in (7, 8, 9, 10)]:
        d0, d1 = legacy.periodo_mes_calendario(ref)
        saida[ref.strftime("%Y-%m")] = seguro(
            "orc", legacy.get_metrics_orcamentos, conn, d0.isoformat(), d1.isoformat(), None, None
        )
    return saida


def _capturar_performance(legacy: Any, conn: Any) -> dict[str, Any]:
    """Contexto de performance do time, sem os graficos.

    ``_performance_build_context`` recebe a referencia como texto ``YYYY-MM``,
    nao como ``date``: internamente faz ``data_ref.strip()``.

    A lista ``rows`` e montada iterando ``set(cur) | set(prev)``, logo sai em
    ordem imprevisivel. Ordenamos por id de vendedor para que a comparacao
    golden-master aponte a linha certa em vez de acusar diferenca so por ordem.
    """
    saida: dict[str, Any] = {}
    for mes in ("2026-08", "2026-09", "2026-10"):
        bruto = seguro("perf", legacy._performance_build_context, conn, mes, True, None)
        if isinstance(bruto, dict) and isinstance(bruto.get("rows"), list):
            bruto["rows"] = sorted(
                bruto["rows"],
                key=lambda r: (str(r.get("id")), str(r.get("nome"))) if isinstance(r, dict) else ("", ""),
            )
        saida[mes] = bruto
    return saida


def capturar_rotas(app: Any, conn_factory: Any) -> dict[str, Any]:
    """Smoke test de todas as rotas GET, com digest dos numeros renderizados.

    Protege a camada de apresentacao: se a extracao de templates mudar um numero
    na tela, o digest muda mesmo que as funcoes de calculo continuem corretas.

    Args:
        app: Aplicacao Flask.
        conn_factory: Callable que devolve conexao com o banco congelado.

    Returns:
        Mapa rota -> status, quantidade de numeros e digest da sequencia.
    """
    with conn_factory() as conn:
        admin = conn.execute(
            "SELECT id FROM users WHERE role='admin' AND ativo=1 ORDER BY id LIMIT 1"
        ).fetchone()
        # Vendedor com mais faturamento, excluindo o canal SITE: /vendedor de um
        # vendedor SITE redireciona para /site por regra de negocio, e uma rota
        # sem dados tambem redireciona, sem exercitar nenhum calculo.
        vendor_id = conn.execute(
            """
            SELECT i.vendor_id AS id FROM invoices i
            JOIN vendors v ON v.id = i.vendor_id
            WHERE i.vendor_id IS NOT NULL AND UPPER(COALESCE(v.tipo,'')) <> 'SITE'
            GROUP BY i.vendor_id ORDER BY COUNT(*) DESC, i.vendor_id ASC LIMIT 1
            """
        ).fetchone()
        cliente_id = conn.execute(
            "SELECT cliente_id FROM invoices WHERE cliente_id IS NOT NULL ORDER BY cliente_id LIMIT 1"
        ).fetchone()
        orc = conn.execute("SELECT numero FROM orcamentos ORDER BY numero LIMIT 1").fetchone()

    substituicoes = {
        "int:vendor_id": str(vendor_id["id"]) if vendor_id else "1",
        "int:cliente_id": str(cliente_id["cliente_id"]) if cliente_id else "1",
        "int:lead_id": "1",
        "int:user_id": "99999",
        "int:import_id": "99999",
        "numero": str(orc["numero"]) if orc else "1",
        "metrica": "orcamentos",
    }

    # Telas que exigem periodo explicito. Sem isso caem no mes corrente, que tem
    # poucos dados, e algumas redirecionam por "vendedor sem dados no periodo".
    query_por_rota = {
        "/admin/performance-time": "?data_ref=2026-09",
        "/admin/performance-time/vendedor/<int:vendor_id>": "?data_ref=2026-09",
        "/admin/performance-time/vendedor/<int:vendor_id>/detalhe/<metrica>": "?data_ref=2026-09",
        "/vendedor/<int:vendor_id>": "?data_ref=2026-09",
        "/dashboard": "?data_ref=2026-09",
        "/buscar": "?q=EMBALAGEM",
    }

    numero_re = re.compile(r"-?\d[\d.,]*")
    saida: dict[str, Any] = {}

    app.config["TESTING"] = True
    client = app.test_client()
    admin_id = int(admin["id"]) if admin else 1

    for regra in sorted(app.url_map.iter_rules(), key=lambda r: str(r.rule)):
        if "GET" not in (regra.methods or set()) or regra.endpoint == "static":
            continue
        # /logout limpa a sessao. Visita-la no meio da varredura derrubava o
        # login e fazia toda rota alfabeticamente posterior responder 302.
        if regra.endpoint == "logout":
            continue
        with client.session_transaction() as sess:
            sess["user_id"] = admin_id
        caminho = str(regra.rule)
        for marcador, valor in substituicoes.items():
            caminho = caminho.replace(f"<{marcador}>", valor)
        if "<" in caminho:
            saida[str(regra.rule)] = {"ignorada": "parametro nao mapeado"}
            continue
        caminho += query_por_rota.get(str(regra.rule), "")
        try:
            resp = client.get(caminho, follow_redirects=False)
            mimetype = (resp.mimetype or "").lower()
            registro: dict[str, Any] = {"status": resp.status_code, "mimetype": mimetype}
            if resp.status_code in (301, 302, 303, 307, 308):
                registro["redireciona_para"] = resp.headers.get("Location")

            textual = mimetype.startswith("text/") or mimetype.endswith(("json", "xml", "html"))
            if textual:
                corpo = resp.get_data(as_text=False).decode("utf-8", errors="replace")
                numeros = numero_re.findall(corpo)
                registro["qtd_numeros"] = len(numeros)
                registro["digest_numeros"] = hashlib.sha256(
                    "|".join(numeros).encode("utf-8")
                ).hexdigest()[:32]
            else:
                # XLSX e um zip com timestamps internos e o nome do arquivo leva
                # datetime.now(): digerir esses bytes seria sempre diferente.
                # Para anexo binario, o que importa e continuar sendo gerado.
                registro["anexo_binario"] = True
                registro["bytes_nao_vazios"] = len(resp.get_data(as_text=False)) > 0
            saida[str(regra.rule)] = registro
        except Exception as exc:  # noqa: BLE001
            saida[str(regra.rule)] = {"__erro__": type(exc).__name__, "mensagem": str(exc)[:200]}
    return saida


def _garantir_hash_seed_fixo() -> None:
    """Reexecuta o processo com PYTHONHASHSEED=0 se ainda nao estiver fixo.

    O monolito percorre ``set`` em varios pontos (por exemplo
    ``all_keys = set(cur) | set(prev)`` em ``_performance_build_context``), e a
    ordem de iteracao de um set de strings depende da semente de hash do
    processo. Sem fixar a semente, duas capturas da mesma base divergem por
    ordenacao e o golden master acusa falso positivo.

    A semente precisa estar definida antes do interpretador subir, por isso a
    unica forma de garanti-la de dentro do script e reexecutar. Usamos
    ``subprocess`` em vez de ``os.execv`` porque no Windows o execv remonta a
    linha de comando e quebra em caminhos com espaco (este projeto vive em
    "D:\\dev npd\\CRM NPD").

    Raises:
        SystemExit: Sempre, no processo pai, propagando o codigo do filho.
    """
    if os.environ.get("PYTHONHASHSEED") == "0":
        return
    ambiente = dict(os.environ, PYTHONHASHSEED="0")
    concluido = subprocess.run(  # noqa: S603
        [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
        env=ambiente,
        cwd=str(PROJECT_ROOT),
        check=False,
    )
    raise SystemExit(concluido.returncode)


def main() -> int:
    """Ponto de entrada da captura."""
    _garantir_hash_seed_fixo()
    parser = argparse.ArgumentParser(description="Captura golden-master do CRM NPD")
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="grava em tests/golden/baseline/ (o baseline versionado)",
    )
    parser.add_argument(
        "--sem-rotas", action="store_true", help="pula o smoke test das rotas HTTP"
    )
    args = parser.parse_args()

    congelar_banco()
    os.environ["CRM_NPD_DB_PATH"] = str(preparar_copia_trabalho())
    os.environ.setdefault("CRM_NPD_CACHE_TTL", "0")

    from tests.golden.freeze import DATA_REFERENCIA, congelar_hoje

    from crm_npd.bootstrap import create_app

    app = create_app()
    import crm_npd.legacy_monolith as legacy
    import crm_npd.services.performance as performance

    with congelar_hoje([legacy, performance], DATA_REFERENCIA) as hoje:
        print(f"Capturando com data de referencia {hoje.isoformat()}...")
        dados = capturar(legacy, hoje)
        if not args.sem_rotas:
            print("Capturando rotas HTTP...")
            dados["rotas"] = capturar_rotas(app, legacy.conectar)

    destino_dir = GOLDEN_DIR / ("baseline" if args.baseline else "output")
    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / "golden.json"
    destino.write_text(
        json.dumps(dados, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tamanho_kb = destino.stat().st_size / 1024
    print(f"Gravado: {destino.relative_to(PROJECT_ROOT)} ({tamanho_kb:.0f} KB)")
    print(f"Secoes: {', '.join(k for k in sorted(dados) if not k.startswith('__'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
