"""CLI para importar dados historicos a partir de arquivos CSV."""

from __future__ import annotations

import argparse
import sys

from crm_npd.db import connect
from crm_npd.config import DB_PATH
from crm_npd.sync.historico import (
    importar_historico_pedidos,
    obter_status_historico_pedidos,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Importador de historico CSV para o CRM NPD")
    parser.add_argument(
        "tipo",
        nargs="?",
        default="pedidos",
        choices=["pedidos", "status"],
        help="Tipo de operacao: pedidos (padrao) ou status",
    )
    parser.add_argument("--arquivo", help="Caminho personalizado para o CSV")
    parser.add_argument("--corte", help="Data de corte YYYY-MM-DD (padrao: 2026-03-01)")

    args = parser.parse_args()

    conn = connect(DB_PATH)
    try:
        if args.tipo == "status":
            st = obter_status_historico_pedidos(conn)
            print("Status do historico de pedidos:")
            for k, v in st.items():
                print(f"  {k}: {v}")
            return

        print(f"Iniciando importacao do historico de pedidos (corte: {args.corte or 'padrao'})...")
        res = importar_historico_pedidos(conn, path=args.arquivo, corte=args.corte)
        print(f"Sucesso: {res['pedidos_gravados']} pedidos gravados (cliches descartados: {res['cliches_descartados']}).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
