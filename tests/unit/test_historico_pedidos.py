import sqlite3
import pytest
from pathlib import Path

from crm_npd.sync.historico import (
    pad_pedido,
    parse_br_date,
    parse_num,
    importar_historico_pedidos,
    obter_status_historico_pedidos,
)
from crm_npd.legacy_monolith import sql_pedidos_realizados_excel_cond
from crm_npd.migrations import apply_safe_migrations


def test_pad_pedido() -> None:
    assert pad_pedido("20") == "00020"
    assert pad_pedido("579") == "00579"
    assert pad_pedido("579/03") == "00579/03"
    assert pad_pedido("23420") == "23420"
    assert pad_pedido("00020") == "00020"
    assert pad_pedido("") == ""
    assert pad_pedido(None) == ""


def test_parse_br_date() -> None:
    assert parse_br_date("23/02/2021") == "2021-02-23"
    assert parse_br_date("01/03/2026") == "2026-03-01"
    assert parse_br_date("2026-03-01") == "2026-03-01"
    assert parse_br_date("") is None
    assert parse_br_date(None) is None


def test_parse_num() -> None:
    assert parse_num("124") == 124.0
    assert parse_num("1,49") == 1.49
    assert parse_num("1.234,56") == 1234.56
    assert parse_num(0) == 0.0
    assert parse_num("") == 0.0
    assert parse_num(None) == 0.0


def test_sql_pedidos_realizados_excel_cond_inclui_98_jit_no_historico() -> None:
    sql = sql_pedidos_realizados_excel_cond("o", "v").upper()
    assert "HISTORICO" in sql
    assert "98%JIT" in sql
    assert "CLICHES" in sql
    assert "CANCELADO" in sql


def test_importar_historico_pedidos_isolado(tmp_path: Path) -> None:
    csv_file = tmp_path / "historicopedidos.csv"
    csv_content = (
        "SITUACAO;TIPO_ITEM_PEDIDO;CONDICAO_ENTREGA;MOTIVO;FANTASIA;EMISSAO_PEDIDO;ENTREGA_PEDIDO;PEDIDO;MEDIDA;VALOR_ITEM;QTDE_TOTAL;REPRESENTATE;RESPONSAVEL;CIDADE;PAPEL;GRUPO;CLIENTE;OBS;UF;PRECO;VALOR_PED;CODIGO;DESCRICAO;DDD;TELEFONE;EMAIL\n"
        "Faturado;Normal;00 - ENTREGAR SOMENTE NO DIA;;CLIENTE A;23/02/2021;26/02/2021;20;22x30x12;124;100;00022 - SITE;;BIGUACU;PAPEL BAGKRAFT;SACOLAS LISAS;04346 - ERICK SEGANFREDO;;SC;1,24;273;04.04.01.0005;SACOLA;48;99651 3842;seraquario@outlook.com\n"
        "Faturado;Normal;98 - JIT;;CLIENTE B;15/04/2021;20/04/2021;579;32x40x12;500;200;05046 - AMANDA;05046 - AMANDA;FLORIANOPOLIS;PAPEL KRAFT;SACOLAS PERSONALIZADAS;05000 - CLIENTE TESTE;;SC;2,50;500;04.04.01.0006;SACOLA JIT;48;99999;teste@email.com\n"
        "Faturado;Normal;03 - IMEDIATA;;CLIENTE C;10/05/2021;12/05/2021;100;CLICHE;150;1;00022 - SITE;;BIGUACU;NENHUM;CLICHES;04346 - ERICK SEGANFREDO;;SC;150;150;99.99;CLICHE ARTE;48;99651;seraquario@outlook.com\n"
        "Faturado;Normal;03 - IMEDIATA;;CLIENTE D;05/03/2026;10/03/2026;99999;22x30x12;200;100;00022 - SITE;;BIGUACU;PAPEL KRAFT;SACOLAS LISAS;04346 - ERICK SEGANFREDO;;SC;2,00;200;04.04.01.0005;SACOLA;48;99651;seraquario@outlook.com\n"
    )
    csv_file.write_text(csv_content, encoding="cp1252")

    # In-memory SQLite with required tables
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE customers (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo TEXT, nome TEXT, fantasia TEXT, cidade TEXT, uf TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE vendors (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo TEXT, nome TEXT, tipo TEXT, ativo INTEGER DEFAULT 1, created_at TEXT, updated_at TEXT);
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER,
            pedido TEXT,
            data_emissao TEXT,
            cliente_id INTEGER,
            vendor_id INTEGER,
            valor REAL,
            quantidade REAL,
            raw_json TEXT,
            created_at TEXT,
            medida_ref TEXT,
            cliente_nome_ref TEXT,
            vendor_nome_ref TEXT,
            dedupe_key TEXT UNIQUE,
            condicao_entrega TEXT,
            is_jit INTEGER DEFAULT 0,
            rede_nome TEXT,
            rede_key TEXT,
            grupo_ref TEXT,
            tipo_item_ref TEXT,
            papel_ref TEXT,
            preco_ref REAL,
            valor_item_ref REAL,
            situacao_pedido TEXT,
            motivo_pedido TEXT,
            desc_motivo_pedido TEXT,
            pedido_cancelado INTEGER DEFAULT 0,
            entrega_pedido TEXT,
            produto_codigo_ref TEXT,
            produto_ref TEXT,
            observacao_ref TEXT,
            cidade TEXT,
            uf TEXT,
            origem_ref TEXT
        );
    """)

    res = importar_historico_pedidos(conn, path=csv_file, corte="2026-03-01")
    assert res["status"] == "sucesso"
    assert res["pedidos_gravados"] == 2  # item 20 e item 579 (cliche e pos-corte descartados)
    assert res["cliches_descartados"] == 1
    assert res["pos_corte_descartados"] == 1

    # Verificar que pedidos foram gravados com padronizacao e origem HISTORICO
    rows = conn.execute("SELECT pedido, origem_ref, valor, condicao_entrega FROM orders ORDER BY pedido").fetchall()
    assert len(rows) == 2
    assert rows[0]["pedido"] == "00020"
    assert rows[0]["origem_ref"] == "HISTORICO"
    assert rows[1]["pedido"] == "00579"
    assert rows[1]["origem_ref"] == "HISTORICO"
    assert "98 - JIT" in rows[1]["condicao_entrega"]

    # Consulta de realizados deve incluir ambos (inclusive o 98-JIT por ser HISTORICO)
    cond = sql_pedidos_realizados_excel_cond("o", "v")
    realizados = conn.execute(f"SELECT COUNT(*) FROM orders o LEFT JOIN vendors v ON v.id = o.vendor_id WHERE {cond}").fetchone()[0]
    assert realizados == 2

    # Idempotência: rodar novamente não duplica nem quebra
    res2 = importar_historico_pedidos(conn, path=csv_file, corte="2026-03-01")
    assert res2["pedidos_gravados"] == 2
    rows2 = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert rows2 == 2


def test_regras_vendedor_historico_anfarma_site_bilyk(tmp_path: Path) -> None:
    """Verifica as 3 regras: ANFARMA -> Paola, Rep=SITE -> SITE, Amanda Bilyk -> Interno."""
    csv_file = tmp_path / "historicopedidos_regras.csv"
    csv_content = (
        "SITUACAO;TIPO_ITEM_PEDIDO;CONDICAO_ENTREGA;MOTIVO;FANTASIA;EMISSAO_PEDIDO;ENTREGA_PEDIDO;PEDIDO;MEDIDA;VALOR_ITEM;QTDE_TOTAL;REPRESENTATE;RESPONSAVEL;CIDADE;PAPEL;GRUPO;CLIENTE;OBS;UF;PRECO;VALOR_PED;CODIGO;DESCRICAO;DDD;TELEFONE;EMAIL\n"
        "Faturado;Normal;00 - DIA;;CLIENTE 1;10/01/2024;15/01/2024;101;22x30x12;1000;500;00022 - SITE;05046 - AMANDA IZABEL BILYK;CIDADE A;PAPEL;SACOLAS;01 - CLI 1;;SC;2.00;1000;01;PROD;48;999;e1\n"
        "Faturado;Normal;00 - DIA;;CLIENTE 2;12/01/2024;15/01/2024;102;22x30x12;2000;1000;05046 - AMANDA IZABEL BILYK;05046 - AMANDA IZABEL BILYK;CIDADE B;PAPEL;SACOLAS;02 - CLI 2;;SC;2.00;2000;02;PROD;48;999;e2\n"
        "Faturado;Normal;00 - DIA;;CLIENTE 3;14/01/2024;15/01/2024;103;22x30x12;3000;1500;05975 - REP;05975 - ASSOCIACAO NACIONAL DE FARMACEUTICOS MAGISTRAIS ANFARMA;CIDADE C;PAPEL;SACOLAS;03 - CLI 3;;SC;2.00;3000;03;PROD;48;999;e3\n"
    )
    csv_file.write_text(csv_content, encoding="cp1252")

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE customers (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo TEXT, nome TEXT, fantasia TEXT, cidade TEXT, uf TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE vendors (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo TEXT, nome TEXT, tipo TEXT, ativo INTEGER DEFAULT 1, created_at TEXT, updated_at TEXT);
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER,
            pedido TEXT,
            data_emissao TEXT,
            cliente_id INTEGER,
            vendor_id INTEGER,
            valor REAL,
            quantidade REAL,
            raw_json TEXT,
            created_at TEXT,
            medida_ref TEXT,
            cliente_nome_ref TEXT,
            vendor_nome_ref TEXT,
            dedupe_key TEXT UNIQUE,
            condicao_entrega TEXT,
            is_jit INTEGER DEFAULT 0,
            rede_nome TEXT,
            rede_key TEXT,
            grupo_ref TEXT,
            tipo_item_ref TEXT,
            papel_ref TEXT,
            preco_ref REAL,
            valor_item_ref REAL,
            situacao_pedido TEXT,
            motivo_pedido TEXT,
            desc_motivo_pedido TEXT,
            pedido_cancelado INTEGER DEFAULT 0,
            entrega_pedido TEXT,
            produto_codigo_ref TEXT,
            produto_ref TEXT,
            observacao_ref TEXT,
            cidade TEXT,
            uf TEXT,
            origem_ref TEXT
        );
    """)

    # Amanda inicializada como Interno
    conn.execute("INSERT INTO vendors (codigo, nome, tipo) VALUES ('05046', 'AMANDA IZABEL BILYK', 'Interno')")
    conn.execute("INSERT INTO vendors (codigo, nome, tipo) VALUES ('05239', 'PAOLA DE MATTOS FRANCISCO', 'Interno')")
    conn.execute("INSERT INTO vendors (codigo, nome, tipo) VALUES ('00022', 'SITE', 'Site')")
    conn.commit()

    importar_historico_pedidos(conn, path=csv_file, corte="2026-03-01")

    # Pedido 101: Representante é SITE -> deve ir para o SITE
    p1 = conn.execute("SELECT o.*, v.nome as v_nome, v.tipo as v_tipo FROM orders o JOIN vendors v ON v.id = o.vendor_id WHERE o.pedido = '00101'").fetchone()
    assert p1["v_nome"] == "SITE"
    assert p1["v_tipo"] == "Site"

    # Pedido 102: Amanda Bilyk com rep nao-site -> fica com Amanda Bilyk (tipo Interno)
    p2 = conn.execute("SELECT o.*, v.nome as v_nome, v.tipo as v_tipo FROM orders o JOIN vendors v ON v.id = o.vendor_id WHERE o.pedido = '00102'").fetchone()
    assert "BILYK" in p2["v_nome"].upper()
    assert p2["v_tipo"] == "Interno"

    # Pedido 103: ANFARMA -> deve ir para Paola de Mattos Francisco (tipo Interno)
    p3 = conn.execute("SELECT o.*, v.nome as v_nome, v.tipo as v_tipo FROM orders o JOIN vendors v ON v.id = o.vendor_id WHERE o.pedido = '00103'").fetchone()
    assert "PAOLA" in p3["v_nome"].upper()
    assert p3["v_tipo"] == "Interno"
