"""Carga oficial: le o FDB e regrava os fatos do CRM.

Uso::

    .venv\\Scripts\\python.exe -m crm_npd.cli.sync_sisplan
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    """Roda a sincronizacao e imprime as contagens."""
    from crm_npd.settings import aplicar_env

    aplicar_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from crm_npd.legacy_monolith import conectar, ensure_users_schema, force_site_vendor_types, init_db
    from crm_npd.migrations import aplicar_migracoes
    from crm_npd.repositories.firebird.conexao import descricao_conexao, disponivel
    from crm_npd.sync.etl import sincronizar_crm

    if not disponivel():
        print("Firebird indisponivel. Confira SISPLAN_HOST, SISPLAN_DATABASE e SISPLAN_PASSWORD no .env.")
        print("Alvo atual:", descricao_conexao())
        return 1

    print("Lendo", descricao_conexao())
    init_db()
    with conectar() as conn:
        ensure_users_schema(conn)
        aplicar_migracoes(conn)
        try:
            force_site_vendor_types(conn)
        except Exception:
            pass
        lidas = sincronizar_crm(conn)
    print("Pedidos:", lidas["pedidos"])
    print("Faturamento:", lidas["faturamento"])
    print("Orcamentos:", lidas["orcamentos"])
    print("Liquidez:", lidas["liquidez"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
