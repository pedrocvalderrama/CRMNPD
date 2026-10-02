"""Conexao somente-leitura com o Firebird do Sisplan.

Nao ha caminho de escrita neste modulo. A transacao e aberta READ ONLY e
toda SQL e rejeitada se nao comecar com SELECT.

Dois modos de DSN:

- **Embedded / arquivo local**: quando ``SISPLAN_HOST`` e ``local``, ``embedded``
  ou vazio e o arquivo ``.FDB`` existe nesta maquina. E o modo da copia
  ``14215F.FDB`` na raiz do projeto.
- **Rede**: ``host/porta:caminho`` contra o servidor do Sisplan.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FDB_LOCAL = PROJECT_ROOT / "14215F.FDB"
FBCLIENT_LOCAL = PROJECT_ROOT / "vendor" / "firebird" / "fbclient.dll"

_PROIBIDO = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|EXECUTE|GRANT|REVOKE|MERGE)\b",
    re.IGNORECASE,
)


class FirebirdSomenteLeituraError(RuntimeError):
    """Levantada quando uma SQL tenta escrever no ERP."""


def _caminho_banco() -> str:
    """Resolve o caminho do ``.FDB``.

    Prefere ``SISPLAN_DATABASE``. Se nao houver, usa a copia na raiz do projeto.
    """
    informado = os.environ.get("SISPLAN_DATABASE", "").strip()
    if informado:
        return informado
    if FDB_LOCAL.exists():
        return str(FDB_LOCAL)
    return r"C:\Sisplan\Base\14215F.FDB"


def _host() -> str:
    """Host do Firebird, ou modo arquivo local."""
    if "SISPLAN_HOST" in os.environ:
        return os.environ.get("SISPLAN_HOST", "").strip()
    if FDB_LOCAL.exists():
        return "local"
    return "192.168.100.168"


def _dsn() -> str:
    """Monta o DSN.

    Returns:
        Caminho do arquivo (embedded) ou ``host/porta:caminho``.
    """
    caminho = _caminho_banco()
    host = _host()
    if host.lower() in ("", "local", "embedded", "file"):
        return caminho
    porta = os.environ.get("SISPLAN_PORT", "3051")
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


def _preparar_cliente() -> str | None:
    """Localiza a DLL e aponta o Firebird para o kit Embedded do projeto.

    Returns:
        Caminho da ``fbclient.dll``, ou None se nao houver.
    """
    dll = os.environ.get("SISPLAN_FBCLIENT", "").strip()
    if not dll or not Path(dll).exists():
        dll = str(FBCLIENT_LOCAL) if FBCLIENT_LOCAL.exists() else ""
    if not dll:
        return None
    raiz = str(Path(dll).resolve().parent)
    os.environ.setdefault("FIREBIRD", raiz)
    os.environ.setdefault("FIREBIRD_LOCK", str(PROJECT_ROOT / "vendor" / "fb_lock"))
    (PROJECT_ROOT / "vendor" / "fb_lock").mkdir(parents=True, exist_ok=True)
    return dll


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

    dll = _preparar_cliente()
    if dll:
        fdb.load_api(dll)

    senha = os.environ.get("SISPLAN_PASSWORD", "")
    if not senha:
        raise RuntimeError(
            "SISPLAN_PASSWORD nao configurada. Copie .env.example para .env e preencha."
        )

    charset = os.environ.get("SISPLAN_CHARSET", "WIN1252")
    ultimo_erro: Exception | None = None
    conn = None
    for tentativa in (charset, "WIN1252", "NONE", "UTF8"):
        try:
            conn = fdb.connect(
                dsn=_dsn(),
                user=os.environ.get("SISPLAN_USER", "SYSDBA"),
                password=senha,
                charset=tentativa,
            )
            break
        except Exception as exc:  # charset errado e o erro mais comum
            ultimo_erro = exc
            conn = None
    if conn is None:
        raise RuntimeError(f"Falha ao abrir o Firebird em {_dsn()}: {ultimo_erro}") from ultimo_erro

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
    """Indica se o cliente, a senha e o arquivo/servidor estao presentes.

    Returns:
        True quando da para tentar uma conexao.
    """
    if not os.environ.get("SISPLAN_PASSWORD"):
        return False
    try:
        import fdb  # noqa: F401
    except ImportError:
        return False
    dll = _preparar_cliente()
    if not dll:
        return False
    host = _host().lower()
    if host in ("", "local", "embedded", "file"):
        return Path(_caminho_banco()).exists()
    return True
