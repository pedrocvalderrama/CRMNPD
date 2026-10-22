"""Diagnostico do ambiente de execucao.

Uso::

    .venv\\Scripts\\python.exe -m crm_npd.cli.doctor
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


def _ok(rotulo: str, ok: bool, detalhe: str = "") -> bool:
    """Imprime uma linha de diagnostico.

    Returns:
        O proprio ``ok``, para acumular o resultado.
    """
    marca = "OK " if ok else "ERRO"
    extra = f" — {detalhe}" if detalhe else ""
    print(f"  [{marca}] {rotulo}{extra}")
    return ok


def main() -> int:
    """Roda as checagens e devolve codigo de saida."""
    from crm_npd.settings import aplicar_env

    aplicar_env()

    print("CRM NPD — diagnostico do ambiente")
    falhas = 0

    venv = Path(sys.executable).resolve()
    if not _ok("Python do venv", ".venv" in str(venv), str(venv)):
        falhas += 1

    try:
        import flask  # noqa: F401
        _ok("Flask instalado", True, flask.__version__)
    except ImportError:
        _ok("Flask instalado", False, "rode ATUALIZAR_DEPENDENCIAS.bat")
        falhas += 1

    env = PROJECT_ROOT / ".env"
    _ok(".env presente", env.exists(), "copie de .env.example" if not env.exists() else "")

    db = Path(os.environ.get("CRM_NPD_DB_PATH") or (PROJECT_ROOT / "crm_npd.db"))
    if db.exists():
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        tabelas = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn.close()
        if not _ok("banco SQLite", {"invoices", "orders", "orcamentos"} <= tabelas, str(db)):
            falhas += 1
    else:
        _ok("banco SQLite", False, f"nao encontrado em {db}")
        falhas += 1

    secret = os.environ.get("CRM_NPD_SECRET", "")
    if not _ok("CRM_NPD_SECRET definido", bool(secret) and secret != "crm_npd_secret_key_change_me"):
        falhas += 1

    from crm_npd.repositories.firebird.conexao import (
        _dll_cliente,
        descricao_conexao,
        executar_select,
    )

    try:
        import fdb  # noqa: F401
    except ImportError:
        fdb = None
    senha = os.environ.get("SISPLAN_PASSWORD", "")
    dll = _dll_cliente() if fdb is not None else None
    _ok("fdb instalado", fdb is not None)
    if dll is not None:
        _ok("fbclient.dll 64-bit", True, str(dll))
    else:
        if not _ok(
            "fbclient.dll 64-bit",
            False,
            "copie C:\\Program Files\\Firebird\\Firebird_3_0\\fbclient.dll para vendor\\firebird",
        ):
            falhas += 1
    _ok("SISPLAN_PASSWORD", bool(senha), "definida" if senha else "preencha no .env")
    _ok("alvo Sisplan", True, descricao_conexao())

    if fdb is None:
        falhas += 1
    if not senha:
        falhas += 1

    if fdb is not None and senha:
        try:
            linhas = executar_select("SELECT 1 FROM RDB$DATABASE")
            _ok("SELECT 1 no Sisplan", linhas == [(1,)] or (linhas and linhas[0][0] == 1), descricao_conexao())
        except Exception as exc:
            _ok("SELECT 1 no Sisplan", False, str(exc)[:200])
            falhas += 1
        else:
            from crm_npd.repositories.firebird.consultas import sql_cdc_max_log, sql_cdc_max_log_alteracao

            try:
                alt = executar_select(sql_cdc_max_log_alteracao())
                log = executar_select(sql_cdc_max_log())
                _ok(
                    "logs CDC (LOG / LOG_ALTERACAO)",
                    True,
                    f"ALT={alt[0][0] if alt else 0} LOG={log[0][0] if log else 0}",
                )
            except Exception as exc:
                _ok("logs CDC (LOG / LOG_ALTERACAO)", False, str(exc)[:200])
                falhas += 1

    print()
    print("Pronto." if falhas == 0 else f"{falhas} ponto(s) exigem acao.")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
