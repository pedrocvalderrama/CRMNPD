"""Prova de conexao SOMENTE LEITURA contra a copia local 14215F.FDB.

Nao grava nada. Se a SQL nao for SELECT, o conector recusa.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SISPLAN_HOST", "local")
os.environ.setdefault("SISPLAN_DATABASE", str(ROOT / "14215F.FDB"))
os.environ.setdefault("SISPLAN_USER", "SYSDBA")
os.environ.setdefault("SISPLAN_FBCLIENT", str(ROOT / "vendor" / "firebird" / "fbclient.dll"))
if not os.environ.get("SISPLAN_PASSWORD"):
    os.environ["SISPLAN_PASSWORD"] = "masterkey"

from crm_npd.repositories.firebird.conexao import (  # noqa: E402
    _dsn,
    conectar_somente_leitura,
    executar_select,
)


def main() -> int:
    print("DSN:", _dsn())
    print("DLL:", os.environ.get("SISPLAN_FBCLIENT"))
    with conectar_somente_leitura() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM RDB$DATABASE")
        print("SELECT 1:", cur.fetchone())
        cur.execute(
            "SELECT COUNT(*) FROM RDB$RELATIONS WHERE RDB$SYSTEM_FLAG = 0 AND RDB$VIEW_BLR IS NULL"
        )
        print("Tabelas de usuario:", cur.fetchone()[0])

    # Faturamento de controle: ciclo 25/08 a 24/09/2026, so leitura.
    sql = """
        SELECT COUNT(*) AS LINHAS,
               CAST(SUM(CAST(NI.VALOR_LIQ AS DOUBLE PRECISION)
                      + CAST(COALESCE(NI.VALOR_FRETE, 0) AS DOUBLE PRECISION)
                      * CASE WHEN NAT.TIPO = 'D' THEN -1 ELSE 1 END) AS DOUBLE PRECISION) AS TOTAL
        FROM NOTAITEN_001 NI
        INNER JOIN NOTA_001 N ON (N.FATURA = NI.FATURA)
        INNER JOIN NATUREZA_001 NAT ON (NAT.NATUREZA = NI.NATUREZA)
        WHERE N.DT_EMISSAO BETWEEN ? AND ?
          AND NAT.TIPO IN ('V', 'D')
          AND (N.MOTIVO_CANCEL IS NULL OR TRIM(N.MOTIVO_CANCEL) = '')
    """
    rows = executar_select(sql, ("2026-08-25", "2026-09-24"))
    print("Faturamento ciclo set/2026 (linhas, total):", rows[0] if rows else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
