import sqlite3
import pytest
from crm_npd.migrations import aplicar_migracoes
from crm_npd.sync.etl import _gravar_faturamento


def test_migracao_adiciona_coluna_valor_frete():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER,
            numero TEXT,
            data_emissao TEXT,
            cliente_id INTEGER,
            vendor_id INTEGER,
            valor REAL DEFAULT 0,
            quantidade REAL,
            pedido TEXT,
            produto TEXT,
            formato TEXT,
            created_at TEXT,
            medida_ref TEXT,
            cliente_nome_ref TEXT,
            vendor_nome_ref TEXT,
            dedupe_key TEXT UNIQUE,
            is_jit INTEGER DEFAULT 0,
            rede_nome TEXT,
            rede_key TEXT,
            grupo_ref TEXT,
            tipo_item_ref TEXT,
            papel_ref TEXT,
            preco_ref REAL,
            valor_item_ref REAL,
            produto_codigo_ref TEXT
        )
    """)
    aplicar_migracoes(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(invoices)").fetchall()}
    assert "valor_frete" in cols


def test_gravar_faturamento_distingue_cliche_e_frete():
    from crm_npd import legacy_monolith as m

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE customers (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo TEXT, nome TEXT, fantasia TEXT, cidade TEXT, uf TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE vendors (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo TEXT, nome TEXT, tipo TEXT, ativo INTEGER DEFAULT 1, created_at TEXT, updated_at TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, pedido TEXT, vendor_id INTEGER, vendor_nome_ref TEXT, quantidade REAL, valor REAL);
        CREATE TABLE invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER,
            numero TEXT,
            data_emissao TEXT,
            cliente_id INTEGER,
            vendor_id INTEGER,
            valor REAL DEFAULT 0,
            quantidade REAL,
            pedido TEXT,
            produto TEXT,
            formato TEXT,
            created_at TEXT,
            medida_ref TEXT,
            cliente_nome_ref TEXT,
            vendor_nome_ref TEXT,
            dedupe_key TEXT UNIQUE,
            is_jit INTEGER DEFAULT 0,
            rede_nome TEXT,
            rede_key TEXT,
            grupo_ref TEXT,
            tipo_item_ref TEXT,
            papel_ref TEXT,
            preco_ref REAL,
            valor_item_ref REAL,
            produto_codigo_ref TEXT,
            valor_frete REAL DEFAULT 0
        );
    """)

    rows = [
        # Linha 1: Mercadoria normal com frete
        {
            "FATURA": "1001",
            "DT_EMISSAO": "2026-10-15",
            "CODCLI": "C01",
            "FANTASIA": "Cliente 1",
            "PEDIDO": "P1",
            "REP_NOME": "Vendedor A",
            "DESCRICAO": "SACOLA KRAFT 30X40",
            "CODIGO": "PROD01",
            "QTDE": 1000,
            "PRECO": 1.50,
            "VALOR_LIQ": 1500.00,
            "VALOR_FRETE": 150.00,
            "MEDIDA": "30X40",
            "GRUPO": "SACOLAS",
            "DESC_MARCA": "PAPEL KRAFT",
            "NAT_TIPO": "V",
            "ORDEM": 1,
        },
        # Linha 2: Item de clichê
        {
            "FATURA": "1001",
            "DT_EMISSAO": "2026-10-15",
            "CODCLI": "C01",
            "FANTASIA": "Cliente 1",
            "PEDIDO": "P1",
            "REP_NOME": "Vendedor A",
            "DESCRICAO": "CLICHE FRENTE/VERSO",
            "CODIGO": "CLI01",
            "QTDE": 1,
            "PRECO": 350.00,
            "VALOR_LIQ": 350.00,
            "VALOR_FRETE": 0.00,
            "MEDIDA": "",
            "GRUPO": "CLICHES",
            "NAT_TIPO": "V",
            "ORDEM": 2,
        },
    ]

    cache = m.build_import_entity_cache(conn)
    n = _gravar_faturamento(conn, m, rows, "2026-10-15T12:00:00", cache)
    assert n == 2
    assert conn.execute("SELECT papel_ref FROM invoices WHERE produto_codigo_ref = ?", ("PROD01",)).fetchone()[0] == "PAPEL KRAFT"

    # Verifica faturamento padrão sem clichê e sem frete
    fat_padrao = conn.execute("""
        SELECT SUM(valor) AS total
        FROM invoices
        WHERE (UPPER(COALESCE(grupo_ref, '')) NOT LIKE '%CLICHE%' AND UPPER(COALESCE(tipo_item_ref, '')) <> 'CLICHE')
    """).fetchone()["total"]
    assert fat_padrao == 1500.00

    # Verifica frete isolado
    frete_tot = conn.execute("SELECT SUM(valor_frete) AS frete FROM invoices").fetchone()["frete"]
    assert frete_tot == 150.00

    # Verifica clichê isolado
    cliche_tot = conn.execute("""
        SELECT SUM(valor) AS cliche
        FROM invoices
        WHERE UPPER(COALESCE(grupo_ref, '')) LIKE '%CLICHE%' OR UPPER(COALESCE(tipo_item_ref, '')) = 'CLICHE'
    """).fetchone()["cliche"]
    assert cliche_tot == 350.00
