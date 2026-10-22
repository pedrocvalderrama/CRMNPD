"""Catalogo somente-leitura do 14215F.FDB.

Gera:
- docs/esquema-firebird/catalogo.json  (todas as tabelas e colunas)
- docs/esquema-firebird/tabelas.tsv    (indice rapido)
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SISPLAN_HOST", "local")
os.environ.setdefault("SISPLAN_DATABASE", str(ROOT / "14215F.FDB"))
os.environ.setdefault("SISPLAN_USER", "SYSDBA")
os.environ.setdefault("SISPLAN_FBCLIENT", str(ROOT / "vendor" / "firebird" / "fbclient.dll"))
if not os.environ.get("SISPLAN_PASSWORD"):
    os.environ["SISPLAN_PASSWORD"] = "masterkey"

from crm_npd.repositories.firebird.conexao import conectar_somente_leitura  # noqa: E402

TIPOS = {
    7: "SMALLINT",
    8: "INTEGER",
    10: "FLOAT",
    12: "DATE",
    13: "TIME",
    14: "CHAR",
    16: "INT64/NUMERIC",
    27: "DOUBLE",
    35: "TIMESTAMP",
    37: "VARCHAR",
    261: "BLOB",
}

SAIDA = ROOT / "docs" / "esquema-firebird"


def _trim(valor) -> str:
    return str(valor or "").strip()


def _tipo(codigo, length, scale, precision, subtype) -> str:
    nome = TIPOS.get(int(codigo or 0), f"TIPO_{codigo}")
    if int(codigo or 0) == 16 and scale not in (None, 0):
        return f"NUMERIC({int(precision or 0)},{abs(int(scale))})"
    if int(codigo or 0) in (14, 37) and length:
        return f"{nome}({int(length)})"
    if int(codigo or 0) == 261 and subtype == 1:
        return "BLOB TEXT"
    return nome


def main() -> int:
    SAIDA.mkdir(parents=True, exist_ok=True)
    with conectar_somente_leitura() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT TRIM(R.RDB$RELATION_NAME)
            FROM RDB$RELATIONS R
            WHERE COALESCE(R.RDB$SYSTEM_FLAG, 0) = 0
              AND R.RDB$VIEW_BLR IS NULL
            ORDER BY 1
        """)
        tabelas = [_trim(r[0]) for r in cur.fetchall() if _trim(r[0])]

        cur.execute("""
            SELECT TRIM(RF.RDB$RELATION_NAME),
                   TRIM(RF.RDB$FIELD_NAME),
                   RF.RDB$FIELD_POSITION,
                   F.RDB$FIELD_TYPE,
                   F.RDB$FIELD_LENGTH,
                   F.RDB$FIELD_SCALE,
                   F.RDB$FIELD_PRECISION,
                   F.RDB$FIELD_SUB_TYPE,
                   RF.RDB$NULL_FLAG
            FROM RDB$RELATION_FIELDS RF
            JOIN RDB$FIELDS F ON F.RDB$FIELD_NAME = RF.RDB$FIELD_SOURCE
            WHERE COALESCE(RF.RDB$SYSTEM_FLAG, 0) = 0
            ORDER BY 1, 3
        """)
        cols = cur.fetchall()

        cur.execute("""
            SELECT TRIM(RC.RDB$RELATION_NAME), TRIM(S.RDB$FIELD_NAME), S.RDB$FIELD_POSITION
            FROM RDB$RELATION_CONSTRAINTS RC
            JOIN RDB$INDEX_SEGMENTS S ON S.RDB$INDEX_NAME = RC.RDB$INDEX_NAME
            WHERE RC.RDB$CONSTRAINT_TYPE = 'PRIMARY KEY'
            ORDER BY 1, 3
        """)
        pks = cur.fetchall()

        cur.execute("""
            SELECT TRIM(RC.RDB$RELATION_NAME),
                   TRIM(S.RDB$FIELD_NAME),
                   TRIM(IDX2.RDB$RELATION_NAME),
                   TRIM(S2.RDB$FIELD_NAME)
            FROM RDB$RELATION_CONSTRAINTS RC
            JOIN RDB$REF_CONSTRAINTS REF ON REF.RDB$CONSTRAINT_NAME = RC.RDB$CONSTRAINT_NAME
            JOIN RDB$INDEX_SEGMENTS S ON S.RDB$INDEX_NAME = RC.RDB$INDEX_NAME
            JOIN RDB$RELATION_CONSTRAINTS RC2 ON RC2.RDB$CONSTRAINT_NAME = REF.RDB$CONST_NAME_UQ
            JOIN RDB$INDICES IDX2 ON IDX2.RDB$INDEX_NAME = RC2.RDB$INDEX_NAME
            JOIN RDB$INDEX_SEGMENTS S2
              ON S2.RDB$INDEX_NAME = RC2.RDB$INDEX_NAME AND S2.RDB$FIELD_POSITION = S.RDB$FIELD_POSITION
            WHERE RC.RDB$CONSTRAINT_TYPE = 'FOREIGN KEY'
            ORDER BY 1, 2
        """)
        fks = cur.fetchall()

        cur.execute("""
            SELECT TRIM(R.RDB$RELATION_NAME)
            FROM RDB$RELATIONS R
            WHERE COALESCE(R.RDB$SYSTEM_FLAG, 0) = 0
              AND R.RDB$VIEW_BLR IS NOT NULL
            ORDER BY 1
        """)
        views = [_trim(r[0]) for r in cur.fetchall() if _trim(r[0])]

    por_tabela: dict[str, dict] = {t: {"colunas": [], "pk": [], "fk": []} for t in tabelas}
    for nome, campo, pos, tipo, length, scale, precision, subtype, nulo in cols:
        tabela = _trim(nome)
        if tabela not in por_tabela:
            continue
        por_tabela[tabela]["colunas"].append({
            "nome": _trim(campo),
            "pos": int(pos or 0),
            "tipo": _tipo(tipo, length, scale, precision, subtype),
            "obrigatorio": bool(nulo),
        })
    for nome, campo, _pos in pks:
        tabela = _trim(nome)
        if tabela in por_tabela:
            por_tabela[tabela]["pk"].append(_trim(campo))
    for origem, campo, destino, campo_dest in fks:
        tabela = _trim(origem)
        if tabela in por_tabela:
            por_tabela[tabela]["fk"].append({
                "coluna": _trim(campo),
                "tabela": _trim(destino),
                "coluna_ref": _trim(campo_dest),
            })

    prefixos: dict[str, list[str]] = defaultdict(list)
    for t in tabelas:
        prefixos[t.split("_")[0]].append(t)

    catalogo = {
        "fonte": str(ROOT / "14215F.FDB"),
        "modo": "embedded-somente-leitura",
        "tabelas": len(tabelas),
        "views": len(views),
        "prefixos": {k: len(v) for k, v in sorted(prefixos.items())},
        "lista_tabelas": tabelas,
        "lista_views": views,
        "detalhe": por_tabela,
    }
    (SAIDA / "catalogo.json").write_text(
        json.dumps(catalogo, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    linhas = ["tabela\tcolunas\tpk\tfk"]
    for t in tabelas:
        info = por_tabela[t]
        linhas.append(
            f"{t}\t{len(info['colunas'])}\t{','.join(info['pk'])}\t"
            + ";".join(f"{f['coluna']}->{f['tabela']}.{f['coluna_ref']}" for f in info["fk"])
        )
    (SAIDA / "tabelas.tsv").write_text("\n".join(linhas), encoding="utf-8")
    print(f"tabelas={len(tabelas)} views={len(views)} json={(SAIDA / 'catalogo.json').stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
