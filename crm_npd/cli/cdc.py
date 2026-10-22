"""Ouvinte CDC do Sisplan: pedidos, faturamento e orcamentos em quase tempo real.

Uso::

    .venv\\Scripts\\python.exe -m crm_npd.cli.cdc
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    """Loop infinito do listener. Ctrl+C encerra."""
    from crm_npd.settings import aplicar_env

    aplicar_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from crm_npd.repositories.firebird.conexao import descricao_conexao, disponivel
    from crm_npd.sync.cdc import loop_cdc

    if not disponivel():
        print("Firebird indisponivel. Confira SISPLAN_HOST, SISPLAN_DATABASE e SISPLAN_PASSWORD no .env.")
        print("Alvo atual:", descricao_conexao())
        return 1

    print("Ouvinte CDC —", descricao_conexao())
    print("Lendo LOG e LOG_ALTERACAO (somente SELECT). Ctrl+C para sair.")
    try:
        loop_cdc()
    except KeyboardInterrupt:
        print("\nOuvinte encerrado.")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
