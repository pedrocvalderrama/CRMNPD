"""Conexao SOMENTE LEITURA com o Firebird 3 do Sisplan.

Parametros oficiais (MANUAL_ACESSO_BANCO_SISPLAN.md):

- host ``192.168.100.168`` (servidor)
- porta ``3051``
- arquivo ``C:\\Sisplan\\Base\\14215F.FDB``
- usuario ``SYSDBA`` / senha no ``.env``
- cliente Firebird: ``fbclient_x64.dll`` (MANUAL_ACESSO_BANCO_SISPLAN.md).

Override local: ``SISPLAN_HOST=local`` + caminho de um FDB copiado.
Nunca INSERT/UPDATE/DELETE/DDL/EXECUTE PROCEDURE.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from crm_npd.settings import PROJECT_ROOT

logger = logging.getLogger(__name__)

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
    """DLL 64-bit do manual: fbclient_x64.dll. O fbclient do Sisplan (32-bit) nao serve."""
    vistos: list[Path] = []
    env = os.environ.get("SISPLAN_FBCLIENT", "").strip()
    if env:
        vistos.append(Path(env))
    vistos.extend(
        (
            PROJECT_ROOT / "fbclient_x64.dll",
            PROJECT_ROOT / "vendor" / "firebird" / "fbclient_x64.dll",
            Path(r"C:\dev\auto_sisplan\fbclient_x64.dll"),
            Path(r"C:\Sisplan\fbclient_x64.dll"),
            Path(r"\\192.168.100.168\Sisplan\fbclient_x64.dll"),
            PROJECT_ROOT / "vendor" / "firebird" / "fbclient.dll",
            Path(r"C:\Program Files\Firebird\Firebird_3_0\fbclient.dll"),
            Path(r"C:\Program Files\Firebird\Firebird_4_0\fbclient.dll"),
            Path(r"C:\Firebird\Firebird_3_0\fbclient.dll"),
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
    """Primeira DLL 64-bit que o Python consegue carregar (Sisplan 32-bit nao serve)."""
    import fdb

    for candidato in _candidatos_fbclient():
        if not candidato.is_file():
            continue
        try:
            fdb.load_api(str(candidato))
            logger.info("fbclient=%s", candidato)
            return candidato
        except Exception:
            logger.warning("fbclient incompativel com Python 64-bit (precisa Firebird 3 x64): %s", candidato)
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
        logger.warning(
            "fbclient_x64.dll nao encontrado (MANUAL_ACESSO_BANCO_SISPLAN.md). "
            "Copie o arquivo para a pasta do CRM ou defina SISPLAN_FBCLIENT no .env."
        )


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
    return {
        "host": _host(),
        "port": _porta(),
        "database": str(caminho_fdb()),
        **comum,
    }


def tpb_monitoramento() -> bytes:
    """TPB Oficial de Baixissimo Impacto (MANUAL_INTEGRACAO_SEGURA_FIREBIRD.md).

    - isc_tpb_version3
    - isc_tpb_read: Nao grava no cabecalho do .FDB nem gera attachment overhead
    - isc_tpb_read_committed + isc_tpb_rec_version: Enxerga mudancas sem prender o OAT
    - isc_tpb_nowait: Se encontrar lock, nao trava o script
    """
    import fdb

    return bytes([
        fdb.isc_tpb_version3,
        fdb.isc_tpb_read,
        fdb.isc_tpb_read_committed,
        fdb.isc_tpb_rec_version,
        fdb.isc_tpb_nowait,
    ])


def conectar_firebird() -> Any:
    """Abre uma conexao TCP persistente com o Firebird do Sisplan."""
    import fdb

    _preparar_cliente()
    return fdb.connect(**_kwargs_conexao())


@contextmanager
def conectar_somente_leitura(conn: Any = None) -> Iterator[Any]:
    """Abre transacao READ ONLY / READ COMMITTED com TPB oficial e commit ao sair.

    Regras obrigatorias do manual:
    - Se conn for passado (reutilizacao de conexao persistente), usa conn sem abrir/fechar.
    - Se conn for None, abre conexao efemera e fecha no final.
    - Em ambos os casos, executa tx.commit() obrigatoriamente para liberar o OAT
      (Garbage Collection do Firebird).
    """
    import fdb

    tpb = tpb_monitoramento()
    fechar_ao_sair = False
    if conn is None or getattr(conn, "closed", True):
        _preparar_cliente()
        conn = fdb.connect(**_kwargs_conexao())
        fechar_ao_sair = True

    tx = conn.trans(tpb)
    try:
        yield tx
        tx.commit()
    except Exception:
        try:
            tx.rollback()
        except Exception:
            pass
        raise
    finally:
        if fechar_ao_sair:
            try:
                conn.close()
            except Exception:
                pass


def executar_select(
    sql: str,
    params: Sequence[Any] | None = None,
    conn: Any = None,
) -> list[tuple[Any, ...]]:
    """Roda um SELECT e devolve as linhas. Qualquer outro SQL e recusado."""
    sql_ok = _validar_select(sql)
    with conectar_somente_leitura(conn=conn) as tx:
        cur = tx.cursor()
        cur.execute(sql_ok, tuple(params or ()))
        return list(cur.fetchall())


def executar_select_dicts(
    sql: str,
    params: Sequence[Any] | None = None,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Roda um SELECT e devolve dicts com o nome da coluna."""
    sql_ok = _validar_select(sql)
    with conectar_somente_leitura(conn=conn) as tx:
        cur = tx.cursor()
        cur.execute(sql_ok, tuple(params or ()))
        nomes = [(col[0] or "").strip() for col in (cur.description or ())]
        return [dict(zip(nomes, linha)) for linha in cur.fetchall()]


def diagnosticar_saude_firebird(conn: Any = None) -> dict[str, Any]:
    """Consulta MON$DATABASE para verificar a saude das transacoes e do OAT (Secao 4 do manual)."""
    from crm_npd.repositories.firebird.consultas import sql_firebird_saude_database

    try:
        rows = executar_select_dicts(sql_firebird_saude_database(), conn=conn)
        if not rows:
            return {}
        row = rows[0]
        oat = int(row.get("OAT") or 0)
        prox = int(row.get("PROXIMA_TRANSACAO") or 0)
        gap = int(row.get("GAP_ACUMULADO") or 0)
        if gap < 1000:
            status = "ok"
            msg = f"Banco operando perfeitamente (GAP={gap} < 1.000)"
        elif gap < 20000:
            status = "atencao"
            msg = f"GAP de transacoes moderado ({gap})"
        else:
            status = "critico"
            msg = f"Alerta critico: GAP={gap} (> 20.000) represando garbage collection"
        return {
            "oat": oat,
            "proxima_transacao": prox,
            "gap_acumulado": gap,
            "status": status,
            "mensagem": msg,
        }
    except Exception as exc:
        return {"status": "erro", "mensagem": str(exc)}


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
