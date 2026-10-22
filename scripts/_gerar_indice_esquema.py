"""Gera indice humano de todas as tabelas a partir do catalogo.json (sem abrir o FDB)."""
from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAT = json.loads((ROOT / "docs" / "esquema-firebird" / "catalogo.json").read_text(encoding="utf-8"))
OUT = ROOT / "docs" / "esquema-firebird" / "indice-por-prefixo.md"

detalhe = CAT["detalhe"]
prefixos: dict[str, list[str]] = defaultdict(list)
for t in CAT["lista_tabelas"]:
    prefixos[t.split("_")[0]].append(t)

linhas = [
    "# Índice das tabelas do 14215F.FDB",
    "",
    f"Fonte: `{CAT['fonte']}` — {CAT['tabelas']} tabelas, {CAT['views']} view (`{', '.join(CAT['lista_views']) or '—'}`).",
    "Catálogo completo de colunas: `docs/esquema-firebird/catalogo.json`.",
    "Índice tabular: `docs/esquema-firebird/tabelas.tsv`.",
    "",
    "O Sisplan **não declara PK/FK** no catálogo (todas vazias). Os joins abaixo são lógicos, pelo nome da coluna.",
    "",
    f"## Prefixos ({len(prefixos)})",
    "",
    "| Prefixo | Tabelas |",
    "|---|---:|",
]
for pref, tabs in sorted(prefixos.items()):
    linhas.append(f"| `{pref}` | {len(tabs)} |")

for pref, tabs in sorted(prefixos.items()):
    linhas.append("")
    linhas.append(f"## {pref} ({len(tabs)})")
    linhas.append("")
    linhas.append("| Tabela | Colunas |")
    linhas.append("|---|---:|")
    for t in tabs:
        linhas.append(f"| `{t}` | {len(detalhe[t]['colunas'])} |")

OUT.write_text("\n".join(linhas) + "\n", encoding="utf-8")
print(f"escrito {OUT} linhas={len(linhas)}")
