"""Listener CDC dos logs nativos do Sisplan (somente SELECT).

A cada poucos segundos le ``LOG_ALTERACAO`` e ``LOG`` com ``ID > cursor``.
Quando o log aponta pedido, nota ou orcamento, o CRM regrava so aquelas
chaves no SQLite. Checkpoint avanca apenas depois do gravar com sucesso.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime

logger = logging.getLogger(__name__)

CHAVE_ALT = "cdc_last_log_alteracao_id"
CHAVE_LOG = "cdc_last_log_id"
CHAVE_ATUALIZADO = "cdc_ultima_atualizacao"

_TABELAS_PEDIDO = (
    "PEDIDO_001",
    "PED_ITEN_001",
    "PEDIDO",
    "PED_ITEN",
)
_TABELAS_FATURA = (
    "NOTA_001",
    "NOTAITEN_001",
    "NOTA_ENTRA_001",
    "NF_ENTRA_ITEN_001",
    "NOTA_ENT",
    "RECEBER_001",
    "RECEBER",
    "NOTA",
    "NOTAITEN",
)
_TABELAS_ORC = (
    "OS_001",
    "OS_ITEN_001",
    "OS",
    "OS_ITEN",
)
_TELAS_PEDIDO = ("PEDIDO", "PEDIDOS")
_TELAS_FATURA = ("FATUR", "NOTA FISCAL", "NOTA DE SAIDA", "NOTA DE ENTRADA", "DUPLICATA", "RECEBER")
_TELAS_ORC = ("ORCAMENT", "ORÇAMENT", "PRE-PEDIDO", "PRE PEDIDO", "OS ", "ORDEM DE SERV")

_RE_EQ = re.compile(
    r"(PED_NUM|NUMERO|PEDIDO|FATURA|NOTAFISCAL|OS)\s*=\s*([0-9A-Za-z.\-_/]+)",
    re.IGNORECASE,
)
_RE_TOKEN = re.compile(r"^[0-9A-Za-z.\-_/]+$")

_cdc_thread: threading.Thread | None = None
_cdc_parar = threading.Event()
_cdc_lock = threading.Lock()


@dataclass
class MudancasCdc:
    pedidos: set[str] = field(default_factory=set)
    faturas: set[str] = field(default_factory=set)
    orcamentos: set[str] = field(default_factory=set)
    last_alt: int = 0
    last_log: int = 0
    lidas_alt: int = 0
    lidas_log: int = 0

    @property
    def tem_alvo(self) -> bool:
        return bool(self.pedidos or self.faturas or self.orcamentos)


def _txt(valor) -> str:
    return str(valor or "").strip()


def _upper(valor) -> str:
    return _txt(valor).upper()


def classificar_tabela(tabela: str | None, tela: str | None) -> str | None:
    """Devolve pedidos, faturamento, orcamentos ou None se o CRM nao usa a tabela."""
    tab = _upper(tabela).replace(" ", "")
    tel = _upper(tela)
    if any(nome == tab or tab.startswith(nome) for nome in _TABELAS_PEDIDO) or any(t in tel for t in _TELAS_PEDIDO):
        if any(t in tel for t in _TELAS_ORC) and "PEDIDO" not in tab:
            return "orcamentos"
        return "pedidos"
    if any(nome == tab or tab.startswith(nome) for nome in _TABELAS_FATURA) or any(t in tel for t in _TELAS_FATURA):
        return "faturamento"
    if any(nome == tab or tab.startswith(nome) for nome in _TABELAS_ORC) or any(t in tel for t in _TELAS_ORC):
        return "orcamentos"
    return None


def extrair_chave_documento(campo: str | None) -> str | None:
    """Tira NUMERO/FATURA/PEDIDO de CAMPO_CHAVE ou CHAVE do log."""
    bruto = _txt(campo)
    if not bruto:
        return None
    match = _RE_EQ.search(bruto)
    if match:
        return match.group(2).strip()
    token = re.split(r"[\s,;]", bruto)[0].strip()
    if token and _RE_TOKEN.match(token):
        return token
    return None


def _intervalo() -> float:
    """Intervalo minimo de 3 a 5 segundos (Regra 5 do manual)."""
    bruto = os.environ.get("SISPLAN_CDC_INTERVAL", "5").strip() or "5"
    try:
        return max(3.0, float(bruto))
    except ValueError:
        return 5.0


def _lote() -> int:
    bruto = os.environ.get("SISPLAN_CDC_BATCH", "2000").strip() or "2000"
    try:
        return max(50, min(int(bruto), 5000))
    except ValueError:
        return 2000


def _cdc_auto() -> bool:
    return os.environ.get("SISPLAN_CDC_AUTO", "").strip().lower() in ("1", "true", "sim", "yes", "on")


def snapshot_desatualizado(synced_at: str, *, hoje: date | None = None) -> bool:
    """True se a ultima carga completa nao for de hoje (base copiada de ontem, etc.)."""
    bruto = (synced_at or "").strip()
    if not bruto:
        return True
    try:
        dt = datetime.fromisoformat(bruto[:19].replace(" ", "T"))
    except ValueError:
        return True
    return dt.date() < (hoje or date.today())


def deve_recarregar_na_subida(
    *,
    origem: str,
    synced_at: str,
    alvo_salvo: str,
    alvo_atual: str,
    modo: str = "stale",
    hoje: date | None = None,
) -> bool:
    """Decide se o Flask deve reler o FDB inteiro antes do ouvinte."""
    if (origem or "").strip().upper() != "FIREBIRD":
        return True
    if (alvo_salvo or "").strip() != (alvo_atual or "").strip():
        return True
    chave = (modo or "stale").strip().lower() or "stale"
    if chave in ("1", "true", "sim", "yes", "on", "always", "sempre"):
        return True
    if chave in ("0", "false", "nao", "no", "off", "never", "nunca"):
        return False
    return snapshot_desatualizado(synced_at, hoje=hoje)


def _max_id(sql: str, fb_conn=None) -> int:
    from crm_npd.repositories.firebird.conexao import executar_select

    linhas = executar_select(sql, conn=fb_conn)
    if not linhas or linhas[0][0] is None:
        return 0
    return int(linhas[0][0] or 0)


def carregar_checkpoint(sqlite_conn, fb_conn=None) -> dict:
    """Le o cursor no SQLite. Cold start pega MAX(ID) atual, sem replay historico."""
    from crm_npd.legacy_monolith import get_setting
    from crm_npd.repositories.firebird.consultas import sql_cdc_max_log, sql_cdc_max_log_alteracao

    alt = get_setting(sqlite_conn, CHAVE_ALT, "").strip()
    log = get_setting(sqlite_conn, CHAVE_LOG, "").strip()
    last_alt = int(alt) if alt else _max_id(sql_cdc_max_log_alteracao(), fb_conn=fb_conn)
    last_log = int(log) if log else _max_id(sql_cdc_max_log(), fb_conn=fb_conn)
    checkpoint = {
        "last_log_alteracao_id": last_alt,
        "last_log_id": last_log,
    }
    if not alt or not log:
        salvar_checkpoint(sqlite_conn, checkpoint)
        logger.info("cdc.cold_start ALT=%s LOG=%s", last_alt, last_log)
    return checkpoint


def alinhar_checkpoint_ao_sisplan(sqlite_conn, fb_conn=None) -> dict:
    """Depois da carga completa, o cursor vai para o MAX(ID) atual (sem replay)."""
    from crm_npd.repositories.firebird.consultas import sql_cdc_max_log, sql_cdc_max_log_alteracao

    checkpoint = {
        "last_log_alteracao_id": _max_id(sql_cdc_max_log_alteracao(), fb_conn=fb_conn),
        "last_log_id": _max_id(sql_cdc_max_log(), fb_conn=fb_conn),
    }
    salvar_checkpoint(sqlite_conn, checkpoint)
    logger.info(
        "cdc.alinhado_apos_sync ALT=%s LOG=%s",
        checkpoint["last_log_alteracao_id"],
        checkpoint["last_log_id"],
    )
    return checkpoint


def salvar_checkpoint(sqlite_conn, checkpoint: dict) -> None:
    from crm_npd.legacy_monolith import set_setting

    set_setting(sqlite_conn, CHAVE_ALT, str(int(checkpoint["last_log_alteracao_id"])))
    set_setting(sqlite_conn, CHAVE_LOG, str(int(checkpoint["last_log_id"])))
    set_setting(sqlite_conn, CHAVE_ATUALIZADO, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def _acumular(mudancas: MudancasCdc, destino: str | None, chave: str | None) -> None:
    if not destino or not chave:
        return
    if destino == "pedidos":
        mudancas.pedidos.add(chave)
    elif destino == "faturamento":
        mudancas.faturas.add(chave)
    elif destino == "orcamentos":
        mudancas.orcamentos.add(chave)


def detectar_mudancas(checkpoint: dict, fb_conn=None) -> MudancasCdc:
    """Le os logs novos com transacao leve isolada e commit imediato (Regras 2, 3 e 4)."""
    from crm_npd.repositories.firebird.conexao import conectar_somente_leitura
    from crm_npd.repositories.firebird.consultas import sql_cdc_log, sql_cdc_log_alteracao

    lote = _lote()
    last_alt = int(checkpoint.get("last_log_alteracao_id") or 0)
    last_log = int(checkpoint.get("last_log_id") or 0)
    mudancas = MudancasCdc(last_alt=last_alt, last_log=last_log)

    with conectar_somente_leitura(conn=fb_conn) as tx:
        cur = tx.cursor()

        # Consulta 1: LOG_ALTERACAO estritamente por ID indexado (Regra 4)
        cur.execute(sql_cdc_log_alteracao(lote), (last_alt,))
        nomes_alt = [(col[0] or "").strip() for col in (cur.description or ())]
        for linha in cur.fetchall():
            row = dict(zip(nomes_alt, linha))
            rid = int(row.get("ID") or 0)
            mudancas.last_alt = max(mudancas.last_alt, rid)
            mudancas.lidas_alt += 1
            destino = classificar_tabela(row.get("TABELA"), row.get("TELA"))
            _acumular(mudancas, destino, extrair_chave_documento(row.get("CAMPO_CHAVE")))

        # Consulta 2: LOG estritamente por ID indexado (Regra 4)
        cur.execute(sql_cdc_log(lote), (last_log,))
        nomes_log = [(col[0] or "").strip() for col in (cur.description or ())]
        for linha in cur.fetchall():
            row = dict(zip(nomes_log, linha))
            rid = int(row.get("ID") or 0)
            mudancas.last_log = max(mudancas.last_log, rid)
            mudancas.lidas_log += 1
            destino = classificar_tabela(row.get("TABELA"), row.get("TELA"))
            chave = extrair_chave_documento(row.get("CHAVE")) or extrair_chave_documento(row.get("DESCRICAO"))
            _acumular(mudancas, destino, chave)

    # Ao sair do bloco, tx.commit() ja foi executado liberando o Garbage Collector (Regra 3)
    return mudancas


def aplicar_mudancas(sqlite_conn, mudancas: MudancasCdc, fb_conn=None) -> bool:
    """Regrava no SQLite as chaves detectadas e so entao avanca o cursor."""
    from crm_npd.legacy_monolith import set_setting
    from crm_npd.services.performance import limpar_caches_comerciais
    from crm_npd.sync.etl import recarregar_faturamento, recarregar_orcamentos, recarregar_pedidos

    try:
        n_ped = recarregar_pedidos(sqlite_conn, mudancas.pedidos, fb_conn=fb_conn)
        n_fat = recarregar_faturamento(
            sqlite_conn,
            faturas=mudancas.faturas,
            pedidos=mudancas.pedidos,
            fb_conn=fb_conn,
        )
        n_orc = recarregar_orcamentos(sqlite_conn, mudancas.orcamentos, fb_conn=fb_conn)
        salvar_checkpoint(
            sqlite_conn,
            {
                "last_log_alteracao_id": mudancas.last_alt,
                "last_log_id": mudancas.last_log,
            },
        )
        set_setting(sqlite_conn, "sisplan_snapshot_synced_at", datetime.now().isoformat(timespec="seconds"))
        sqlite_conn.commit()
        limpar_caches_comerciais()
        logger.info(
            "cdc.ok ped=%s fat=%s orc=%s cursor ALT=%s LOG=%s",
            n_ped,
            n_fat,
            n_orc,
            mudancas.last_alt,
            mudancas.last_log,
        )
        return True
    except Exception:
        logger.exception("cdc.falha ao gravar no CRM; cursor nao avancou")
        try:
            sqlite_conn.rollback()
        except Exception:
            pass
        return False


def ciclo(fb_conn=None) -> MudancasCdc | None:
    """Um passo do listener. Devolve as mudancas vistas, ou None se Sisplan indisponivel."""
    from crm_npd.legacy_monolith import conectar
    from crm_npd.repositories.firebird.conexao import disponivel

    if not disponivel():
        logger.warning("cdc.sisplan indisponivel; conferir SISPLAN_* no .env")
        return None

    with conectar() as sqlite_conn:
        checkpoint = carregar_checkpoint(sqlite_conn, fb_conn=fb_conn)
        sqlite_conn.commit()
        mudancas = detectar_mudancas(checkpoint, fb_conn=fb_conn)
        avanco = (
            mudancas.last_alt != int(checkpoint["last_log_alteracao_id"])
            or mudancas.last_log != int(checkpoint["last_log_id"])
        )
        if mudancas.tem_alvo:
            logger.info(
                "cdc.detectado pedidos=%s faturas=%s orcamentos=%s",
                sorted(mudancas.pedidos),
                sorted(mudancas.faturas),
                sorted(mudancas.orcamentos),
            )
            aplicar_mudancas(sqlite_conn, mudancas, fb_conn=fb_conn)
        elif avanco:
            salvar_checkpoint(
                sqlite_conn,
                {
                    "last_log_alteracao_id": mudancas.last_alt,
                    "last_log_id": mudancas.last_log,
                },
            )
            sqlite_conn.commit()
        return mudancas


def loop_cdc(*, uma_vez: bool = False) -> None:
    """Loop continuo com conexao persistente unica (Regras 1, 2, 3, 4 e 5)."""
    import fdb
    from crm_npd.legacy_monolith import ensure_users_schema, init_db
    from crm_npd.migrations import aplicar_migracoes
    from crm_npd.repositories.firebird.conexao import (
        conectar_firebird,
        descricao_conexao,
        diagnosticar_saude_firebird,
        disponivel,
    )

    intervalo = _intervalo()
    logger.info("cdc.inicio alvo=%s intervalo=%.1fs", descricao_conexao(), intervalo)

    # Inicializa tabelas e migracoes no SQLite uma unica vez na subida
    try:
        init_db()
        from crm_npd.legacy_monolith import conectar
        with conectar() as s_conn:
            ensure_users_schema(s_conn)
            aplicar_migracoes(s_conn)
            s_conn.commit()
    except Exception as exc:
        logger.warning("cdc.init_db falhou na inicializacao: %s", exc)

    fb_conn = None
    ciclos_cont = 0

    while not _cdc_parar.is_set():
        try:
            if not disponivel():
                logger.warning("cdc.sisplan indisponivel; aguardando no intervalo...")
                _cdc_parar.wait(intervalo)
                if uma_vez:
                    break
                continue

            # Regra 1: Abre conexao persistente unica e reutiliza nos ciclos
            if fb_conn is None or getattr(fb_conn, "closed", True):
                logger.info("cdc.conectando conexao persistente ao Firebird do Sisplan...")
                fb_conn = conectar_firebird()

                # Auditoria de saude na conexao inicial
                try:
                    saude = diagnosticar_saude_firebird(conn=fb_conn)
                    if saude and saude.get("status") != "erro":
                        logger.info(
                            "cdc.saude_firebird OAT=%s NEXT=%s GAP=%s (%s)",
                            saude.get("oat"),
                            saude.get("proxima_transacao"),
                            saude.get("gap_acumulado"),
                            saude.get("mensagem"),
                        )
                except Exception:
                    pass

            mudancas = ciclo(fb_conn=fb_conn)
            ciclos_cont += 1

            # A cada 100 ciclos (~5 a 8 minutos), valida saude do OAT e GAP
            if ciclos_cont % 100 == 0:
                try:
                    saude = diagnosticar_saude_firebird(conn=fb_conn)
                    if saude and saude.get("status") in ("atencao", "critico"):
                        logger.warning(
                            "cdc.alerta_saude_firebird GAP=%s status=%s (%s)",
                            saude.get("gap_acumulado"),
                            saude.get("status"),
                            saude.get("mensagem"),
                        )
                except Exception:
                    pass

            if uma_vez:
                return
            if mudancas and (mudancas.lidas_alt >= _lote() or mudancas.lidas_log >= _lote()):
                continue
        except fdb.Error as fb_err:
            logger.error("cdc.erro_firebird: %s. Reconectando controladamente no proximo ciclo...", fb_err)
            if fb_conn is not None:
                try:
                    fb_conn.close()
                except Exception:
                    pass
                fb_conn = None
            if uma_vez:
                raise
        except Exception as exc:
            logger.exception("cdc.ciclo falhou: %s", exc)
            if fb_conn is not None:
                try:
                    fb_conn.close()
                except Exception:
                    pass
                fb_conn = None
            if uma_vez:
                raise
        finally:
            if uma_vez and fb_conn is not None:
                try:
                    fb_conn.close()
                except Exception:
                    pass
                fb_conn = None

        _cdc_parar.wait(intervalo)
        if uma_vez:
            break

    if fb_conn is not None:
        try:
            fb_conn.close()
        except Exception:
            pass


def iniciar_cdc_em_background() -> bool:
    """Sobe o listener numa thread daemon (opcional, SISPLAN_CDC_AUTO=1)."""
    global _cdc_thread
    from crm_npd.sync.fonte import usando_firebird

    if not usando_firebird():
        logger.info("cdc.auto ignorado: CRM_NPD_FONTE=sqlite")
        return False
    if not _cdc_auto():
        return False
    from crm_npd.repositories.firebird.conexao import disponivel

    if not disponivel():
        logger.warning("cdc.auto ignorado: Sisplan indisponivel")
        return False
    with _cdc_lock:
        if _cdc_thread is not None and _cdc_thread.is_alive():
            return True
        _cdc_parar.clear()
        _cdc_thread = threading.Thread(target=loop_cdc, name="cdc-sisplan", daemon=True)
        _cdc_thread.start()
        logger.info("cdc.auto thread iniciada")
        return True
