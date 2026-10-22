"""Prova de conexao SOMENTE LEITURA no Sisplan da fabrica.

Usa o .env (host 192.168.100.168 porta 3051, C:\\Sisplan\\Base\\14215F.FDB).
Nao grava nada. Se a SQL nao for SELECT, o conector recusa.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crm_npd.repositories.firebird.conexao import (  # noqa: E402
    _dll_cliente,
    _kwargs_conexao,
    descricao_conexao,
    executar_select,
)
from crm_npd.settings import aplicar_env  # noqa: E402

aplicar_env()


def main() -> int:
    kwargs = _kwargs_conexao()
    print("Alvo:", descricao_conexao())
    print("DLL:", _dll_cliente())
    print("Connect:", {k: v for k, v in kwargs.items() if k != "password"})
    rows = executar_select("SELECT 1 FROM RDB$DATABASE")
    print("SELECT 1:", rows)
    tabelas = executar_select(
        "SELECT COUNT(*) FROM RDB$RELATIONS WHERE RDB$SYSTEM_FLAG = 0 AND RDB$VIEW_BLR IS NULL"
    )
    print("Tabelas de usuario:", tabelas[0][0] if tabelas else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
