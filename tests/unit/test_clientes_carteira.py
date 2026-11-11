"""Regras de atribuição exclusiva da carteira na página /clientes."""

from __future__ import annotations

import sqlite3

from crm_npd import legacy_monolith as m


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE vendors (
            id INTEGER PRIMARY KEY,
            codigo TEXT,
            nome TEXT
        );
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            codigo TEXT,
            nome TEXT,
            fantasia TEXT
        );
        CREATE TABLE customer_identity_rules (
            customer_id INTEGER PRIMARY KEY,
            identity_key TEXT,
            identity_nome TEXT,
            tipo_vinculo TEXT,
            matriz_customer_id INTEGER
        );
        CREATE TABLE invoices (
            id INTEGER PRIMARY KEY,
            cliente_id INTEGER,
            vendor_id INTEGER,
            data_emissao TEXT
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            cliente_id INTEGER,
            vendor_id INTEGER,
            data_emissao TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO vendors(id, codigo, nome) VALUES (?, ?, ?)",
        [(1, "00101", "Ana"), (2, "00202", "Beatriz")],
    )
    conn.executemany(
        "INSERT INTO customers(id, codigo, nome, fantasia) VALUES (?, ?, ?, ?)",
        [(10, "C10", "Acme Matriz", "Acme"), (11, "C11", "Acme Filial", "Acme Filial")],
    )
    conn.executemany(
        """INSERT INTO customer_identity_rules(
               customer_id, identity_key, identity_nome, tipo_vinculo, matriz_customer_id
           ) VALUES (?, 'MATRIZ:10', 'Acme', ?, 10)""",
        [(10, "MATRIZ"), (11, "FILIAL")],
    )
    return conn


def _user(codigo: str, nome: str) -> dict:
    return {"role": "user", "vendor_codigo": codigo, "nome": nome}


def test_identidade_aparece_somente_para_ultimo_responsavel() -> None:
    conn = _conn()
    conn.executemany(
        "INSERT INTO invoices(id, cliente_id, vendor_id, data_emissao) VALUES (?, ?, ?, ?)",
        [
            (1, 10, 1, "2026-01-10"),
            (2, 11, 2, "2026-02-10"),
        ],
    )

    carteira_ana = m.list_customer_identities_for_user(conn, _user("101", "Ana"))
    carteira_beatriz = m.list_customer_identities_for_user(conn, _user("00202", "Beatriz"))

    assert carteira_ana == []
    assert [item["identity_key"] for item in carteira_beatriz] == ["MATRIZ:10"]
    conn.close()


def test_pedido_mais_recente_transfere_identidade_inteira() -> None:
    conn = _conn()
    conn.executemany(
        "INSERT INTO invoices(id, cliente_id, vendor_id, data_emissao) VALUES (?, ?, ?, ?)",
        [
            (1, 10, 1, "2026-01-10"),
            (2, 11, 2, "2026-02-10"),
        ],
    )
    conn.execute(
        "INSERT INTO orders(id, cliente_id, vendor_id, data_emissao) VALUES (?, ?, ?, ?)",
        (3, 11, 1, "2026-03-10"),
    )

    carteira_ana = m.list_customer_identities_for_user(conn, _user("00101", "Ana"))
    carteira_beatriz = m.list_customer_identities_for_user(conn, _user("202", "Beatriz"))

    assert [item["identity_key"] for item in carteira_ana] == ["MATRIZ:10"]
    assert carteira_beatriz == []
    conn.close()


def test_pedido_prevalece_sobre_nota_quando_movimentos_sao_do_mesmo_dia() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO invoices(id, cliente_id, vendor_id, data_emissao) VALUES (?, ?, ?, ?)",
        (99, 10, 2, "2026-03-10"),
    )
    conn.execute(
        "INSERT INTO orders(id, cliente_id, vendor_id, data_emissao) VALUES (?, ?, ?, ?)",
        (1, 11, 1, "2026-03-10"),
    )

    carteira_ana = m.list_customer_identities_for_user(conn, _user("101", "Ana"))
    carteira_beatriz = m.list_customer_identities_for_user(conn, _user("202", "Beatriz"))

    assert [item["identity_key"] for item in carteira_ana] == ["MATRIZ:10"]
    assert carteira_beatriz == []
    conn.close()


def test_admin_continua_visualizando_todas_as_identidades() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO invoices(id, cliente_id, vendor_id, data_emissao) VALUES (?, ?, ?, ?)",
        (1, 10, 1, "2026-01-10"),
    )

    carteira = m.list_customer_identities_for_user(
        conn,
        {"role": "admin", "vendor_codigo": "", "nome": "Admin"},
    )

    assert [item["identity_key"] for item in carteira] == ["MATRIZ:10"]
    conn.close()
