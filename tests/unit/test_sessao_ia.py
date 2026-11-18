"""Regressões de sessão: cookie pequeno e histórico da IA no servidor."""

from __future__ import annotations

import os
import sqlite3

import pytest
from werkzeug.security import generate_password_hash

from crm_npd import legacy_monolith as m
from crm_npd.bootstrap import create_app


@pytest.fixture()
def session_client(tmp_path, monkeypatch):
    db_path = tmp_path / "sessao.db"

    monkeypatch.setenv("CRM_NPD_FONTE", "sqlite")
    monkeypatch.setenv("SISPLAN_CDC_AUTO", "0")
    monkeypatch.setenv("SISPLAN_SYNC_ON_START", "0")
    monkeypatch.setenv("ML_SHEETS_SYNC_ON_START", "0")
    monkeypatch.setenv("ML_SHEETS_SYNC_AUTO", "0")

    def conectar_teste():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    app = create_app()
    monkeypatch.setattr(m, "conectar", conectar_teste)
    with conectar_teste() as conn:
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
            CREATE TABLE ia_chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                pergunta TEXT NOT NULL,
                resposta TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """INSERT INTO users(
                   id, nome, email, role, senha_hash, ativo, vendor_codigo, created_at
               ) VALUES (1, 'Usuária Teste', 'sessao@teste.local', 'user', ?, 1, '00101', ?)""",
            (generate_password_hash("senha-atual"), m.now_iso()),
        )
        conn.commit()

    monkeypatch.setattr(m, "init_db", lambda: None)
    app.config.update(TESTING=True)
    return app.test_client(), db_path, conectar_teste


def _autenticar(client) -> None:
    with client.session_transaction() as sess:
        sess["user_id"] = 1


def test_configuracao_usa_cookie_exclusivo_e_chave_do_ambiente(session_client) -> None:
    client, _, _ = session_client
    app = client.application

    assert app.config["SESSION_COOKIE_NAME"] == "crm_npd_session"
    assert app.permanent_session_lifetime.days == 7
    assert app.secret_key == os.environ["CRM_NPD_SECRET"]


def test_login_limpa_historico_legado_e_cria_sessao_permanente(session_client) -> None:
    client, _, _ = session_client
    with client.session_transaction() as sess:
        sess["ia_chat_history"] = [{"pergunta": "antiga", "resposta": "x" * 5000}]

    response = client.post(
        "/login",
        data={"email": "sessao@teste.local", "senha": "senha-atual"},
        follow_redirects=False,
    )

    with client.session_transaction() as sess:
        assert sess["user_id"] == 1
        assert sess.permanent is True
        assert "ia_chat_history" not in sess

    cookie = response.headers.get("Set-Cookie", "")
    assert cookie.startswith("crm_npd_session=")
    assert "Expires=" in cookie
    assert len(cookie.encode("utf-8")) < 1000


def test_resposta_grande_da_ia_fica_no_banco_e_nao_no_cookie(session_client, monkeypatch) -> None:
    client, _, conectar_teste = session_client
    _autenticar(client)
    resposta_grande = "Resposta segura. " + ("conteúdo detalhado " * 1200)
    monkeypatch.setattr(m, "responder_ia_crm", lambda pergunta, user=None: resposta_grande)

    response = client.post(
        "/ia",
        data={"pergunta": "Faça uma análise completa"},
        follow_redirects=False,
    )

    with client.session_transaction() as sess:
        assert sess["user_id"] == 1
        assert sess.permanent is True
        assert "ia_chat_history" not in sess

    cookie = response.headers.get("Set-Cookie", "")
    assert len(cookie.encode("utf-8")) < 1000
    with conectar_teste() as conn:
        row = conn.execute(
            "SELECT pergunta, resposta FROM ia_chat_history WHERE user_id=1"
        ).fetchone()
    assert row["pergunta"] == "Faça uma análise completa"
    assert row["resposta"] == resposta_grande


def test_historico_conserva_somente_as_ultimas_vinte_mensagens(session_client) -> None:
    _, _, conectar_teste = session_client
    with conectar_teste() as conn:
        for numero in range(25):
            m._ia_chat_history_add(conn, 1, f"Pergunta {numero}", f"Resposta {numero}")
        conn.commit()
        historico = m._ia_chat_history_load(conn, 1)

    assert len(historico) == 20
    assert historico[0]["pergunta"] == "Pergunta 5"
    assert historico[-1]["pergunta"] == "Pergunta 24"


def test_limpar_conversa_remove_somente_historico_do_usuario(session_client) -> None:
    client, _, conectar_teste = session_client
    _autenticar(client)
    with conectar_teste() as conn:
        m._ia_chat_history_add(conn, 1, "Pergunta", "Resposta")
        conn.commit()

    response = client.post("/ia", data={"acao": "limpar"}, follow_redirects=False)

    assert response.status_code in (302, 303)
    with conectar_teste() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ia_chat_history WHERE user_id=1"
        ).fetchone()[0] == 0
