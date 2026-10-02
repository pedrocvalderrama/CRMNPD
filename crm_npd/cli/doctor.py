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

    try:
        import fdb  # noqa: F401
        tem_fdb = True
    except ImportError:
        tem_fdb = False
    dll = os.environ.get("SISPLAN_FBCLIENT", r"C:\dev\auto_sisplan\fbclient_x64.dll")
    senha = os.environ.get("SISPLAN_PASSWORD", "")
    _ok("fdb instalado", tem_fdb, "opcional ate o cutover")
    _ok("fbclient.dll", Path(dll).exists(), dll)
    _ok("SISPLAN_PASSWORD", bool(senha), "obrigatorio so para sync Firebird")

    print()
    print("Pronto." if falhas == 0 else f"{falhas} ponto(s) exigem acao.")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
