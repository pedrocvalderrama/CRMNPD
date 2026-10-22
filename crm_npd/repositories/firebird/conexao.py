"""Conexao SOMENTE LEITURA com o Firebird 3 do Sisplan.

Parametros oficiais (MANUAL_ACESSO_BANCO_SISPLAN.md):

- host ``192.168.100.168`` (servidor)
- porta ``3051``
- arquivo ``C:\\Sisplan\\Base\\14215F.FDB``
- usuario ``SYSDBA`` / senha no ``.env``
- cliente Firebird: ``vendor/firebird/fbclient.dll`` no projeto, ou o cliente
  ja instalado no Windows. Nao depende de ``C:\\dev\\auto_sisplan``.

Override local: ``SISPLAN_HOST=local`` + caminho de um FDB copiado.
Nunca INSERT/UPDATE/DELETE/DDL/EXECUTE PROCEDURE.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from crm_npd.settings import PROJECT_ROOT

# Manual de acesso ao banco Sisplan (fabrica).
HOST_FABRICA = "192.168.100.168"
PORTA_FABRICA = 3051
DATABASE_FABRICA = r"C:\Sisplan\Base\14215F.FDB"
USUARIO_PADRAO = "SYSDBA"
CHARSET_PADRAO = "WIN1252"

_SQL_PROIBIDO = (
    "INSERT ",
    "UPDATE ",
    "DELETE ",
    "MERGE ",
    "CREATE ",
    "ALTER ",
    "DROP ",
    "GRANT ",
    "REVOKE ",
    "EXECUTE ",
    "TRUNCATE ",
)


class FirebirdSomenteLeituraError(RuntimeError):
    """Tentativa de emitir SQL que nao e SELECT."""


def _modo_local(host: str | None = None) -> bool:
    """True quando o FDB e um arquivo local (Embedded), nao a rede da fabrica."""
    valor = (host if host is not None else os.environ.get("SISPLAN_HOST", HOST_FABRICA)).strip().lower()
    return valor in ("", "local", "embedded", "file")


def _host() -> str:
    return os.environ.get("SISPLAN_HOST", HOST_FABRICA).strip() or HOST_FABRICA


def _porta() -> int:
    bruto = os.environ.get("SISPLAN_PORT", str(PORTA_FABRICA)).strip() or str(PORTA_FABRICA)
    try:
        return int(bruto)
    except ValueError:
        return PORTA_FABRICA


def caminho_fdb() -> Path:
    """Caminho do arquivo FDB no servidor (ou da copia local)."""
    bruto = os.environ.get("SISPLAN_DATABASE", DATABASE_FABRICA).strip()
    return Path(bruto or DATABASE_FABRICA)


def _candidatos_fbclient() -> list[Path]:
    """Ordem: .env (opcional), vendor do projeto, Firebird instalado no Windows."""
    vistos: list[Path] = []
    env = os.environ.get("SISPLAN_FBCLIENT", "").strip()
    if env:
        vistos.append(Path(env))
    vistos.extend(
        (
            PROJECT_ROOT / "vendor" / "firebird" / "fbclient.dll",
            PROJECT_ROOT / "vendor" / "firebird" / "fbclient_x64.dll",
            Path(r"C:\Program Files\Firebird\Firebird_3_0\fbclient.dll"),
            Path(r"C:\Program Files\Firebird\Firebird_4_0\fbclient.dll"),
            Path(r"C:\Windows\System32\fbclient.dll"),
        )
    )
    unicos: list[Path] = []
    ja: set[str] = set()
    for item in vistos:
        chave = str(item).lower()
        if chave in ja:
            continue
        ja.add(chave)
        unicos.append(item)
    return unicos


def _dll_cliente() -> Path | None:
    for candidato in _candidatos_fbclient():
        if candidato.is_file():
            return candidato
    return None


def _dsn() -> str:
    """DSN Firebird. Mantido para diagnostico; a conexao de rede usa host+porta+database."""
    caminho = str(caminho_fdb())
    if _modo_local():
        return caminho
    return f"{_host()}/{_porta()}:{caminho}"


def descricao_conexao() -> str:
    """Texto curto para a tela Atualizar Sisplan."""
    if _modo_local():
        return f"arquivo local {caminho_fdb()}"
    return f"{_host()}:{_porta()} -> {caminho_fdb()}"


def _validar_select(sql: str) -> str:
    texto = sql.strip()
    if not texto:
        raise FirebirdSomenteLeituraError("SQL vazio.")
    if ";" in texto.rstrip().rstrip(";"):
        raise FirebirdSomenteLeituraError("Apenas um comando SELECT por chamada.")
    upper = texto.upper()
    if not upper.startswith("SELECT"):
        raise FirebirdSomenteLeituraError("Somente SELECT e permitido neste conector.")
    for token in _SQL_PROIBIDO:
        if token in upper:
            raise FirebirdSomenteLeituraError(f"Comando proibido no conector Sisplan: {token.strip()}")
    return texto


def _preparar_cliente() -> None:
    dll = _dll_cliente()
    if dll is None:
        return
    import fdb

    fdb.load_api(str(dll))


def _kwargs_conexao() -> dict[str, Any]:
    """Argumentos de ``fdb.connect`` iguais ao exemplo do manual (rede) ou Embedded (local)."""
    senha = os.environ.get("SISPLAN_PASSWORD", "")
    usuario = os.environ.get("SISPLAN_USER", USUARIO_PADRAO)
    charset = os.environ.get("SISPLAN_CHARSET", CHARSET_PADRAO)
    comum = {
        "user": usuario,
        "password": senha,
        "charset": charset,
    }
    if _modo_local():
        return {"dsn": str(caminho_fdb()), **comum}
    # Barra invertida + "C:" no DSN vira "unavailable database" em alguns fbclient.
    caminho = str(caminho_fdb()).replace("\\", "/")
    return {
        "host": _host(),
        "port": _porta(),
        "database": caminho,
        **comum,
    }


@contextmanager
def conectar_somente_leitura() -> Iterator[Any]:
    """Abre o Firebird em transacao READ ONLY / READ COMMITTED e faz rollback ao sair."""
    import fdb

    _preparar_cliente()
    conn = fdb.connect(**_kwargs_conexao())
    tpb = (
        fdb.isc_tpb_read,
        fdb.isc_tpb_read_committed,
        fdb.isc_tpb_rec_version,
        fdb.isc_tpb_wait,
    )
    transacao_aberta = False
    try:
        conn.begin(tpb=tpb)
        transacao_aberta = True
        yield conn
    finally:
        if transacao_aberta:
            try:
                conn.rollback()
            except Exception:
                pass
        conn.close()


def executar_select(sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
    """Roda um SELECT e devolve as linhas. Qualquer outro SQL e recusado."""
    sql_ok = _validar_select(sql)
    with conectar_somente_leitura() as conn:
        cur = conn.cursor()
        cur.execute(sql_ok, tuple(params or ()))
        return list(cur.fetchall())


def executar_select_dicts(sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    """Roda um SELECT e devolve dicts com o nome da coluna."""
    sql_ok = _validar_select(sql)
    with conectar_somente_leitura() as conn:
        cur = conn.cursor()
        cur.execute(sql_ok, tuple(params or ()))
        nomes = [(col[0] or "").strip() for col in (cur.description or ())]
        return [dict(zip(nomes, linha)) for linha in cur.fetchall()]


def disponivel() -> bool:
    """True se da para tentar abrir o Sisplan (senha + cliente fdb + DLL ou modo rede)."""
    if not os.environ.get("SISPLAN_PASSWORD"):
        return False
    try:
        import fdb  # noqa: F401
    except ImportError:
        return False
    if _dll_cliente() is None and _modo_local():
        return False
    if _modo_local() and not caminho_fdb().is_file():
        return False
    return True
