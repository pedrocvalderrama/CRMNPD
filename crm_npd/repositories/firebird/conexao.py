"""Conexao somente-leitura com o Firebird do Sisplan.

Nao ha caminho de escrita neste modulo. A transacao e aberta READ ONLY e
toda SQL e rejeitada se nao comecar com SELECT.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_PROIBIDO = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|EXECUTE|GRANT|REVOKE|MERGE)\b",
    re.IGNORECASE,
)


class FirebirdSomenteLeituraError(RuntimeError):
    """Levantada quando uma SQL tenta escrever no ERP."""


def _dsn() -> str:
    """Monta o DSN a partir das variaveis de ambiente.

    Returns:
        DSN no formato host/porta:caminho.
    """
    host = os.environ.get("SISPLAN_HOST", "192.168.100.168")
    porta = os.environ.get("SISPLAN_PORT", "3051")
    caminho = os.environ.get("SISPLAN_DATABASE", r"C:\Sisplan\Base\14215F.FDB")
    return f"{host}/{porta}:{caminho}"


def _validar_select(sql: str) -> str:
    """Garante que a SQL e um SELECT puro.

    Args:
        sql: Comando candidato.

    Returns:
        SQL original, se valida.

    Raises:
        FirebirdSomenteLeituraError: Se a SQL nao for SELECT ou contiver
            verbo de escrita.
    """
    compacta = " ".join((sql or "").strip().split())
    if not compacta.upper().startswith("SELECT"):
        raise FirebirdSomenteLeituraError(
            "Apenas SELECT e permitido contra o Sisplan. "
            "O banco 14215F.FDB e o ERP de producao da empresa."
        )
    if _PROIBIDO.search(compacta):
        raise FirebirdSomenteLeituraError(
            "A SQL contem verbo de escrita e foi bloqueada. "
            "O CRM nunca grava no Firebird."
        )
    return sql


@contextmanager
def conectar_somente_leitura() -> Iterator[Any]:
    """Abre conexao Firebird em transacao READ ONLY / READ COMMITTED.

    Yields:
        Conexao fdb pronta para SELECT.

    Raises:
        RuntimeError: Se o cliente Firebird nao estiver instalado.
    """
    try:
        import fdb  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "O pacote fdb nao esta instalado. Rode ATUALIZAR_DEPENDENCIAS.bat."
        ) from exc

    dll = os.environ.get("SISPLAN_FBCLIENT", r"C:\dev\auto_sisplan\fbclient_x64.dll")
    if os.path.exists(dll):
        fdb.load_api(dll)

    senha = os.environ.get("SISPLAN_PASSWORD", "")
    if not senha:
        raise RuntimeError(
            "SISPLAN_PASSWORD nao configurada. Copie .env.example para .env e preencha."
        )

    conn = fdb.connect(
        dsn=_dsn(),
        user=os.environ.get("SISPLAN_USER", "SYSDBA"),
        password=senha,
        charset="UTF8",
    )
    try:
        try:
            tpb = fdb.TPB()
            tpb.access_mode = fdb.isc_tpb_read
            tpb.isolation_level = fdb.isc_tpb_read_committed
            conn.begin(tpb=tpb)
        except Exception:
            conn.begin()
        yield conn
        conn.rollback()
    finally:
        conn.close()


def executar_select(sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
    """Executa um SELECT no Sisplan e devolve as linhas.

    Args:
        sql: Consulta, obrigatoriamente SELECT.
        params: Parametros posicionais.

    Returns:
        Lista de tuplas.

    Raises:
        FirebirdSomenteLeituraError: Se a SQL nao for SELECT.
    """
    _validar_select(sql)
    with conectar_somente_leitura() as conn:
        cur = conn.cursor()
        cur.execute(sql, tuple(params or ()))
        rows = cur.fetchall()
        logger.info("firebird.select linhas=%s", len(rows))
        return list(rows)


def disponivel() -> bool:
    """Indica se o cliente e as credenciais minimas estao presentes.

    Returns:
        True quando ha senha configurada e o modulo fdb importa.
    """
    if not os.environ.get("SISPLAN_PASSWORD"):
        return False
    try:
        import fdb  # noqa: F401
    except ImportError:
        return False
    return True
