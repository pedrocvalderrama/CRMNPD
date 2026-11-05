"""Testes unitários para a rota /desempenho (toggle faturamento/pedidos e site)."""

from __future__ import annotations

import pytest

from crm_npd.bootstrap import create_app


@pytest.fixture(scope="module")
def admin_client():
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    login = client.post(
        "/login",
        data={"email": "admin@npd.local", "senha": "npdadmin"},
        follow_redirects=False,
    )
    assert login.status_code in (302, 303)
    return client


def test_desempenho_padrao_faturamento(admin_client) -> None:
    """Verifica que a tela de desempenho abre por padrão em faturamento sem site."""
    resp = admin_client.get("/desempenho")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Tabs e botões
    assert "Desempenho comercial" in html
    assert "Comparativo de faturamento" in html
    assert "Mostrar Site" in html
    assert "bi-square" in html  # desmarcado por padrão
    assert "bi-check-square-fill" not in html
    assert "Faturamento" in html
    assert "notas" in html


def test_desempenho_aba_pedidos(admin_client) -> None:
    """Verifica que a tela de desempenho em visão pedidos renderiza os rótulos de pedidos."""
    resp = admin_client.get("/desempenho?visao=pedidos")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "Comparativo de pedidos realizados" in html
    assert "Volume de pedidos" in html
    assert "Quantidade pedida" in html
    assert "por pedido" in html
    assert "Evolução dos pedidos" in html
    assert "Pedidos mensais" in html
    assert "bi-square" in html  # site continua desmarcado por padrão


def test_desempenho_toggle_site(admin_client) -> None:
    """Verifica que o botão de site ativa e desativa mantendo a visão."""
    # Ativa site em faturamento
    resp_fat_site = admin_client.get("/desempenho?visao=faturamento&site=1")
    assert resp_fat_site.status_code == 200
    html_fat_site = resp_fat_site.get_data(as_text=True)
    assert "bi-check-square-fill" in html_fat_site
    assert "Site" in html_fat_site

    # Ativa site em pedidos
    resp_ped_site = admin_client.get("/desempenho?visao=pedidos&site=1")
    assert resp_ped_site.status_code == 200
    html_ped_site = resp_ped_site.get_data(as_text=True)
    assert "bi-check-square-fill" in html_ped_site
    assert "Volume de pedidos" in html_ped_site
    assert "Site" in html_ped_site


def test_desempenho_filtros_preservam_estado(admin_client) -> None:
    """Verifica que submissão de filtros mantém os parâmetros ocultos de visão e site."""
    resp = admin_client.get("/desempenho?visao=pedidos&site=1&equipe=EXTERNO")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '<input type="hidden" name="visao" value="pedidos">' in html
    assert '<input type="hidden" name="site" value="1">' in html


def test_regras_duais_site_fev_e_marco() -> None:
    """Garante as regras duais:
    - Fev/2026 para tras: NFs de Amanda Bilyk e Ana Beatriz contam como SITE (regra de antes).
    - Mar/2026 para frente: Amanda Bilyk e equipe interna; apenas pedidos/NFs do SITE vao para o SITE.
    """
    import sqlite3
    from datetime import date
    from crm_npd import legacy_monolith as m

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE vendors (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo TEXT, nome TEXT, tipo TEXT, ativo INTEGER DEFAULT 1);
        CREATE TABLE invoices (id INTEGER PRIMARY KEY AUTOINCREMENT, numero TEXT, data_emissao TEXT, vendor_id INTEGER, valor REAL, quantidade REAL, cliente_id INTEGER, vendor_nome_ref TEXT);
    """)
    conn.execute("INSERT INTO vendors (id, codigo, nome, tipo) VALUES (1, '05046', 'AMANDA IZABEL BILYK', 'Interno')")
    conn.execute("INSERT INTO vendors (id, codigo, nome, tipo) VALUES (34, '03052', 'ANA BEATRIZ ABDU MENEZES', 'Site')")
    conn.execute("INSERT INTO vendors (id, codigo, nome, tipo) VALUES (49, '00022', 'SITE', 'Site')")

    # Invoices em fev/2026
    conn.execute("INSERT INTO invoices (numero, data_emissao, vendor_id, valor, quantidade) VALUES ('F1', '2026-02-10', 1, 100.0, 10)")
    # Invoices em marco/2026
    conn.execute("INSERT INTO invoices (numero, data_emissao, vendor_id, valor, quantidade) VALUES ('F2', '2026-03-10', 1, 200.0, 20)")
    conn.execute("INSERT INTO invoices (numero, data_emissao, vendor_id, valor, quantidade) VALUES ('F3', '2026-03-15', 49, 300.0, 30)")
    conn.commit()

    # Em fev/2026: F1 (Amanda Bilyk) conta como SITE
    site_fev = conn.execute(f"""
        SELECT COALESCE(SUM(i.valor), 0) AS val, COUNT(*) AS qtd
        FROM invoices i
        LEFT JOIN vendors v ON v.id = i.vendor_id
        WHERE {m.sql_site_match('i', join_alias='v')}
          AND date(i.data_emissao) BETWEEN '2026-02-01' AND '2026-02-28'
    """, m.site_sql_params(49)).fetchone()
    assert site_fev["val"] == 100.0
    assert site_fev["qtd"] == 1

    # Em marco/2026: F2 (Amanda Bilyk) NAO e site, apenas F3 (SITE)
    site_mar = conn.execute(f"""
        SELECT COALESCE(SUM(i.valor), 0) AS val, COUNT(*) AS qtd
        FROM invoices i
        LEFT JOIN vendors v ON v.id = i.vendor_id
        WHERE {m.sql_site_match('i', join_alias='v')}
          AND date(i.data_emissao) BETWEEN '2026-03-01' AND '2026-03-31'
    """, m.site_sql_params(49)).fetchone()
    assert site_mar["val"] == 300.0
    assert site_mar["qtd"] == 1
