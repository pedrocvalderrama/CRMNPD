"""Testes da alteração da própria senha e das permissões do menu Sistema."""

from __future__ import annotations

import sqlite3

import pytest
from werkzeug.security import check_password_hash, generate_password_hash

from crm_npd import legacy_monolith as m
from crm_npd.bootstrap import create_app


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    db_path = tmp_path / "usuarios.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            nome TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL,
            senha_hash TEXT NOT NULL,
            ativo INTEGER NOT NULL,
            vendor_codigo TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.executemany(
        """INSERT INTO users(
               id, nome, email, role, senha_hash, ativo, vendor_codigo, created_at
           ) VALUES (?, ?, ?, ?, ?, 1, ?, '2026-11-17')""",
        [
            (1, "Vendedora Teste", "vendedora@teste.local", "user", generate_password_hash("senha-atual"), "00101"),
            (2, "Admin Teste", "admin@teste.local", "admin", generate_password_hash("senha-admin"), None),
        ],
    )
    conn.commit()
    conn.close()

    def conectar_teste():
        test_conn = sqlite3.connect(db_path)
        test_conn.row_factory = sqlite3.Row
        return test_conn

    app = create_app()
    monkeypatch.setattr(m, "conectar", conectar_teste)
    app.config.update(TESTING=True, SECRET_KEY="teste-mudar-senha")
    client = app.test_client()
    return client, db_path


def _autenticar(client, user_id: int) -> None:
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def _hash_atual(db_path, user_id: int = 1) -> str:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT senha_hash FROM users WHERE id=?", (user_id,)).fetchone()[0]
    finally:
        conn.close()


def test_usuario_comum_ve_somente_mudar_senha_no_sistema(app_client) -> None:
    client, _ = app_client
    _autenticar(client, 1)

    response = client.get("/sistema/mudar-senha")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Sistema" in html
    assert "Mudar senha" in html
    assert "Atualizar Sisplan" not in html
    assert ">Importações<" not in html
    assert ">Usuários<" not in html
    assert ">Metas<" not in html


def test_admin_mantem_links_do_sistema_e_recebe_mudar_senha(app_client) -> None:
    client, _ = app_client
    _autenticar(client, 2)

    response = client.get("/sistema/mudar-senha")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Atualizar Sisplan" in html
    assert "Importações" in html
    assert "Usuários" in html
    assert "Metas" in html
    assert "Mudar senha" in html


def test_senha_atual_incorreta_nao_altera_hash(app_client) -> None:
    client, db_path = app_client
    _autenticar(client, 1)
    hash_anterior = _hash_atual(db_path)

    response = client.post(
        "/sistema/mudar-senha",
        data={
            "senha_atual": "incorreta",
            "nova_senha": "senha-nova",
            "confirmar_senha": "senha-nova",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "A senha atual está incorreta." in response.get_data(as_text=True)
    assert _hash_atual(db_path) == hash_anterior


def test_confirmacao_divergente_nao_altera_hash(app_client) -> None:
    client, db_path = app_client
    _autenticar(client, 1)
    hash_anterior = _hash_atual(db_path)

    response = client.post(
        "/sistema/mudar-senha",
        data={
            "senha_atual": "senha-atual",
            "nova_senha": "senha-nova",
            "confirmar_senha": "outra-senha",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "A confirmação da nova senha não confere." in response.get_data(as_text=True)
    assert _hash_atual(db_path) == hash_anterior


def test_usuario_altera_a_propria_senha(app_client) -> None:
    client, db_path = app_client
    _autenticar(client, 1)

    response = client.post(
        "/sistema/mudar-senha",
        data={
            "senha_atual": "senha-atual",
            "nova_senha": "senha-nova",
            "confirmar_senha": "senha-nova",
        },
        follow_redirects=True,
    )

    novo_hash = _hash_atual(db_path)
    assert response.status_code == 200
    assert "Senha alterada com sucesso." in response.get_data(as_text=True)
    assert check_password_hash(novo_hash, "senha-nova")
    assert not check_password_hash(novo_hash, "senha-atual")
