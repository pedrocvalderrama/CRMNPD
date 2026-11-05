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


def test_regras_unificadas_site_amanda_ana_beatriz() -> None:
    """Garante as regras unificadas (fev para tras e marco para frente):
    - Pedidos/NFs com representante Site contam sempre para o SITE independente do responsavel.
    - Caso o representante nao for site, conta para o responsavel.
    - Amanda Bilyk e Ana Beatriz quando o representante nao for Site contam como Interno.
    """
    import sqlite3
    from datetime import date
    from crm_npd import legacy_monolith as m

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE vendors (id INTEGER PRIMARY KEY AUTOINCREMENT, codigo TEXT, nome TEXT, tipo TEXT, ativo INTEGER DEFAULT 1);
        CREATE TABLE invoices (id INTEGER PRIMARY KEY AUTOINCREMENT, numero TEXT, data_emissao TEXT, vendor_id INTEGER, valor REAL, quantidade REAL, cliente_id INTEGER, vendor_nome_ref TEXT, pedido TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, pedido TEXT, data_emissao TEXT, vendor_id INTEGER, valor REAL, valor_item_ref REAL, quantidade REAL, cliente_id INTEGER, vendor_nome_ref TEXT, pedido_cancelado INTEGER DEFAULT 0, condicao_entrega TEXT, motivo_pedido TEXT, desc_motivo_pedido TEXT);
    """)
    conn.execute("INSERT INTO vendors (id, codigo, nome, tipo) VALUES (1, '05046', 'AMANDA IZABEL BILYK', 'Interno')")
    conn.execute("INSERT INTO vendors (id, codigo, nome, tipo) VALUES (34, '03052', 'ANA BEATRIZ ABDU MENEZES', 'Interno')")
    conn.execute("INSERT INTO vendors (id, codigo, nome, tipo) VALUES (49, '00022', 'SITE', 'Site')")

    # Pedido com representante Site -> vendor_id = 49 (SITE)
    conn.execute("INSERT INTO orders (pedido, data_emissao, vendor_id, valor, valor_item_ref, quantidade, vendor_nome_ref) VALUES ('PED-SITE', '2026-02-10', 49, 150.0, 150.0, 1, 'SITE')")
    # Pedido da Amanda Bilyk onde representante nao e Site -> vendor_id = 1 (Interno)
    conn.execute("INSERT INTO orders (pedido, data_emissao, vendor_id, valor, valor_item_ref, quantidade, vendor_nome_ref) VALUES ('PED-AMANDA', '2026-02-10', 1, 250.0, 250.0, 1, 'AMANDA IZABEL BILYK')")
    # Pedido da Ana Beatriz onde representante nao e Site -> vendor_id = 34 (Interno)
    conn.execute("INSERT INTO orders (pedido, data_emissao, vendor_id, valor, valor_item_ref, quantidade, vendor_nome_ref) VALUES ('PED-ANA', '2026-03-10', 34, 350.0, 350.0, 1, 'ANA BEATRIZ ABDU MENEZES')")

    # Invoices correspondentes
    conn.execute("INSERT INTO invoices (numero, data_emissao, vendor_id, valor, quantidade, pedido, vendor_nome_ref) VALUES ('NF-SITE', '2026-02-12', 49, 150.0, 1, 'PED-SITE', 'SITE')")
    conn.execute("INSERT INTO invoices (numero, data_emissao, vendor_id, valor, quantidade, pedido, vendor_nome_ref) VALUES ('NF-AMANDA', '2026-02-12', 1, 250.0, 1, 'PED-AMANDA', 'AMANDA IZABEL BILYK')")
    conn.execute("INSERT INTO invoices (numero, data_emissao, vendor_id, valor, quantidade, pedido, vendor_nome_ref) VALUES ('NF-ANA', '2026-03-12', 34, 350.0, 1, 'PED-ANA', 'ANA BEATRIZ ABDU MENEZES')")
    conn.commit()

    # SITE query
    site_fev = conn.execute(f"""
        SELECT COALESCE(SUM(i.valor), 0) AS val, COUNT(*) AS qtd
        FROM invoices i
        LEFT JOIN vendors v ON v.id = i.vendor_id
        WHERE {m.sql_site_match('i', join_alias='v')}
          AND date(i.data_emissao) BETWEEN '2026-02-01' AND '2026-02-28'
    """, m.site_sql_params(49)).fetchone()
    assert site_fev["val"] == 150.0
    assert site_fev["qtd"] == 1

    # INTERNO query em fev: pega Amanda Bilyk (250.0) e exclui SITE
    interno_fev = conn.execute(f"""
        SELECT COALESCE(SUM(i.valor), 0) AS val, COUNT(*) AS qtd
        FROM invoices i
        LEFT JOIN vendors v ON v.id = i.vendor_id
        WHERE {m.sql_site_exclusion_cond('v')}
          AND v.tipo = 'Interno'
          AND date(i.data_emissao) BETWEEN '2026-02-01' AND '2026-02-28'
    """).fetchone()
    assert interno_fev["val"] == 250.0
    assert interno_fev["qtd"] == 1

    # INTERNO query em marco: pega Ana Beatriz (350.0) e exclui SITE
    interno_mar = conn.execute(f"""
        SELECT COALESCE(SUM(i.valor), 0) AS val, COUNT(*) AS qtd
        FROM invoices i
        LEFT JOIN vendors v ON v.id = i.vendor_id
        WHERE {m.sql_site_exclusion_cond('v')}
          AND v.tipo = 'Interno'
          AND date(i.data_emissao) BETWEEN '2026-03-01' AND '2026-03-31'
    """).fetchone()
    assert interno_mar["val"] == 350.0
    assert interno_mar["qtd"] == 1
