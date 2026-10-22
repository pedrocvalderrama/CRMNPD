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
    bruto = os.environ.get("SISPLAN_CDC_INTERVAL", "5").strip() or "5"
    try:
        return max(2.0, float(bruto))
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


def _max_id(sql: str) -> int:
    from crm_npd.repositories.firebird.conexao import executar_select

    linhas = executar_select(sql)
    if not linhas or linhas[0][0] is None:
        return 0
    return int(linhas[0][0] or 0)


def carregar_checkpoint(sqlite_conn) -> dict:
    """Le o cursor no SQLite. Cold start pega MAX(ID) atual, sem replay historico."""
    from crm_npd.legacy_monolith import get_setting
    from crm_npd.repositories.firebird.consultas import sql_cdc_max_log, sql_cdc_max_log_alteracao

    alt = get_setting(sqlite_conn, CHAVE_ALT, "").strip()
    log = get_setting(sqlite_conn, CHAVE_LOG, "").strip()
    last_alt = int(alt) if alt else _max_id(sql_cdc_max_log_alteracao())
    last_log = int(log) if log else _max_id(sql_cdc_max_log())
    checkpoint = {
        "last_log_alteracao_id": last_alt,
        "last_log_id": last_log,
    }
    if not alt or not log:
        salvar_checkpoint(sqlite_conn, checkpoint)
        logger.info("cdc.cold_start ALT=%s LOG=%s", last_alt, last_log)
    return checkpoint


def alinhar_checkpoint_ao_sisplan(sqlite_conn) -> dict:
    """Depois da carga completa, o cursor vai para o MAX(ID) atual (sem replay)."""
    from crm_npd.repositories.firebird.consultas import sql_cdc_max_log, sql_cdc_max_log_alteracao

    checkpoint = {
        "last_log_alteracao_id": _max_id(sql_cdc_max_log_alteracao()),
        "last_log_id": _max_id(sql_cdc_max_log()),
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


def detectar_mudancas(checkpoint: dict) -> MudancasCdc:
    """Le os logs novos. Nao grava nada no Sisplan."""
    from crm_npd.repositories.firebird.conexao import executar_select_dicts
    from crm_npd.repositories.firebird.consultas import sql_cdc_log, sql_cdc_log_alteracao

    lote = _lote()
    last_alt = int(checkpoint.get("last_log_alteracao_id") or 0)
    last_log = int(checkpoint.get("last_log_id") or 0)
    mudancas = MudancasCdc(last_alt=last_alt, last_log=last_log)

    alts = executar_select_dicts(sql_cdc_log_alteracao(lote), (last_alt,))
    for row in alts:
        rid = int(row.get("ID") or 0)
        mudancas.last_alt = max(mudancas.last_alt, rid)
        mudancas.lidas_alt += 1
        destino = classificar_tabela(row.get("TABELA"), row.get("TELA"))
        _acumular(mudancas, destino, extrair_chave_documento(row.get("CAMPO_CHAVE")))

    logs = executar_select_dicts(sql_cdc_log(lote), (last_log,))
    for row in logs:
        rid = int(row.get("ID") or 0)
        mudancas.last_log = max(mudancas.last_log, rid)
        mudancas.lidas_log += 1
        destino = classificar_tabela(row.get("TABELA"), row.get("TELA"))
        chave = extrair_chave_documento(row.get("CHAVE")) or extrair_chave_documento(row.get("DESCRICAO"))
        _acumular(mudancas, destino, chave)

    return mudancas


def aplicar_mudancas(sqlite_conn, mudancas: MudancasCdc) -> bool:
    """Regrava no SQLite as chaves detectadas e so entao avanca o cursor."""
    from crm_npd.legacy_monolith import set_setting
    from crm_npd.services.performance import limpar_caches_comerciais
    from crm_npd.sync.etl import recarregar_faturamento, recarregar_orcamentos, recarregar_pedidos

    try:
        n_ped = recarregar_pedidos(sqlite_conn, mudancas.pedidos)
        n_fat = recarregar_faturamento(
            sqlite_conn,
            faturas=mudancas.faturas,
            pedidos=mudancas.pedidos,
        )
        n_orc = recarregar_orcamentos(sqlite_conn, mudancas.orcamentos)
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


def ciclo() -> MudancasCdc | None:
    """Um passo do listener. Devolve as mudancas vistas, ou None se Sisplan indisponivel."""
    from crm_npd.legacy_monolith import conectar, ensure_users_schema, init_db
    from crm_npd.migrations import aplicar_migracoes
    from crm_npd.repositories.firebird.conexao import disponivel

    if not disponivel():
        logger.warning("cdc.sisplan indisponivel; conferir SISPLAN_* no .env")
        return None

    init_db()
    with conectar() as sqlite_conn:
        ensure_users_schema(sqlite_conn)
        aplicar_migracoes(sqlite_conn)
        checkpoint = carregar_checkpoint(sqlite_conn)
        sqlite_conn.commit()
        mudancas = detectar_mudancas(checkpoint)
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
            aplicar_mudancas(sqlite_conn, mudancas)
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
    """Loop continuo. ``uma_vez`` existe para teste e para o doctor."""
    from crm_npd.repositories.firebird.conexao import descricao_conexao

    logger.info("cdc.inicio alvo=%s intervalo=%ss", descricao_conexao(), _intervalo())
    while not _cdc_parar.is_set():
        try:
            mudancas = ciclo()
            if uma_vez:
                return
            if mudancas and (mudancas.lidas_alt >= _lote() or mudancas.lidas_log >= _lote()):
                continue
        except Exception:
            logger.exception("cdc.ciclo falhou; nova tentativa no intervalo")
            if uma_vez:
                raise
        _cdc_parar.wait(_intervalo())
        if uma_vez:
            break


def iniciar_cdc_em_background() -> bool:
    """Sobe o listener numa thread daemon (opcional, SISPLAN_CDC_AUTO=1)."""
    global _cdc_thread
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
