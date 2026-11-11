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


def test_ticket_medio_modo_nota_e_item() -> None:
    """Verifica que o ticket medio pode ser calculado por nota ou por item."""
    import sqlite3
    from datetime import date
    from crm_npd import legacy_monolith as m

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE customers (id INTEGER PRIMARY KEY, nome TEXT);
        CREATE TABLE vendors (id INTEGER PRIMARY KEY, nome TEXT, tipo TEXT);
        CREATE TABLE invoices (id INTEGER PRIMARY KEY, numero TEXT, data_emissao TEXT, vendor_id INTEGER, valor REAL, quantidade REAL, cliente_id INTEGER);
    """)
    conn.execute("INSERT INTO vendors (id, nome, tipo) VALUES (1, 'V1', 'Interno')")
    # Nota 1 tem 2 itens (linhas)
    conn.execute("INSERT INTO invoices (numero, data_emissao, vendor_id, valor, quantidade, cliente_id) VALUES ('NF1', '2026-10-05', 1, 60.0, 5, 10)")
    conn.execute("INSERT INTO invoices (numero, data_emissao, vendor_id, valor, quantidade, cliente_id) VALUES ('NF1', '2026-10-05', 1, 40.0, 5, 10)")
    # Nota 2 tem 1 item (linha)
    conn.execute("INSERT INTO invoices (numero, data_emissao, vendor_id, valor, quantidade, cliente_id) VALUES ('NF2', '2026-10-10', 1, 100.0, 10, 20)")
    conn.commit()

    val, qtd_notas, clientes, unidades, itens = m._soma_faturamento_periodo(
        conn, date(2026, 10, 1), date(2026, 10, 31)
    )
    assert val == 200.0
    assert qtd_notas == 2   # 2 notas fiscais distintas
    assert itens == 3       # 3 itens/linhas faturadas
    assert unidades == 20.0 # 20 pecas
    assert clientes == 2

    # Ticket por nota
    ticket_nota = val / qtd_notas
    assert ticket_nota == 100.0

    # Ticket por item
    ticket_item = val / itens
    assert round(ticket_item, 2) == 66.67

    # Ticket por quantidade (unidades fisicas)
    ticket_quantidade = val / unidades
    assert ticket_quantidade == 10.0


def test_devolucao_desconta_notas_itens_e_quantidade_faturada() -> None:
    """Devolução reduz os denominadores físicos da tela de desempenho."""
    import sqlite3
    from datetime import date
    from crm_npd import legacy_monolith as m

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE customers (id INTEGER PRIMARY KEY, nome TEXT);
        CREATE TABLE vendors (id INTEGER PRIMARY KEY, nome TEXT, tipo TEXT);
        CREATE TABLE invoices (
            id INTEGER PRIMARY KEY,
            numero TEXT,
            data_emissao TEXT,
            vendor_id INTEGER,
            valor REAL,
            quantidade REAL,
            cliente_id INTEGER,
            tipo_item_ref TEXT
        );
    """)
    conn.execute("INSERT INTO vendors (id, nome, tipo) VALUES (1, 'V1', 'INTERNO')")
    conn.executemany(
        """INSERT INTO invoices
           (numero, data_emissao, vendor_id, valor, quantidade, cliente_id, tipo_item_ref)
           VALUES (?, '2026-10-05', 1, ?, ?, 10, ?)""",
        [
            ("NF1", 60.0, 5.0, "V"),
            ("NF1", 40.0, 5.0, "V"),
            ("NF2", 100.0, 10.0, "V"),
            ("DEV1", -40.0, 4.0, "D"),
        ],
    )
    conn.commit()

    valor, notas, _clientes, unidades, itens = m._soma_faturamento_periodo(
        conn, date(2026, 10, 1), date(2026, 10, 31)
    )

    assert valor == 160.0
    assert notas == 1       # 2 notas de venda - 1 nota de devolução
    assert unidades == 16.0  # 20 peças vendidas - 4 devolvidas
    assert itens == 2       # 3 linhas de venda - 1 linha devolvida


def test_migracao_qtde_demo_por_pedidos() -> None:
    """Verifica que DEMO busca quantidade real do pedido e divide proporcionalmente entre parcelas."""
    import sqlite3
    from crm_npd.migrations import _atualizar_qtde_demo_por_pedidos

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            pedido TEXT,
            quantidade REAL,
            valor REAL
        );
        CREATE TABLE invoices (
            id INTEGER PRIMARY KEY,
            numero TEXT,
            tipo_item_ref TEXT,
            grupo_ref TEXT,
            pedido TEXT,
            valor REAL,
            quantidade REAL,
            preco_ref REAL
        );
    """)

    # Pedido 1001 com 6000 sacolas e R$ 3000,00 de valor total
    conn.execute("INSERT INTO orders (pedido, quantidade, valor) VALUES ('1001', 6000.0, 3000.0)")
    # DEMO com 2 parcelas de R$ 1500 cada (50% cada)
    conn.execute("INSERT INTO invoices (id, numero, tipo_item_ref, pedido, valor, quantidade) VALUES (1, 'DEMO-001-1', 'DEMO', '1001', 1500.0, 1.0)")
    conn.execute("INSERT INTO invoices (id, numero, tipo_item_ref, pedido, valor, quantidade) VALUES (2, 'DEMO-001-2', 'DEMO', '1001', 1500.0, 1.0)")

    conn.commit()
    _atualizar_qtde_demo_por_pedidos(conn)

    inv1 = conn.execute("SELECT * FROM invoices WHERE id = 1").fetchone()
    inv2 = conn.execute("SELECT * FROM invoices WHERE id = 2").fetchone()

    # Cada parcela deve receber 3000 sacolas (50% das 6000) e preco unitario 0.50
    assert inv1["quantidade"] == 3000.0
    assert inv1["preco_ref"] == 0.5
    assert inv2["quantidade"] == 3000.0
    assert inv2["preco_ref"] == 0.5


def test_desempenho_frete_cliche_e_mercado_livre() -> None:
    """Verifica que os botoes/toggles de frete/cliche e mercado livre refletem corretamente no desempenho."""
    import sqlite3
    from datetime import date
    from crm_npd import legacy_monolith as m

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE customers (id INTEGER PRIMARY KEY, nome TEXT);
        CREATE TABLE vendors (id INTEGER PRIMARY KEY, nome TEXT, tipo TEXT);
        CREATE TABLE invoices (
            id INTEGER PRIMARY KEY,
            numero TEXT,
            data_emissao TEXT,
            vendor_id INTEGER,
            valor REAL,
            valor_frete REAL,
            quantidade REAL,
            cliente_id INTEGER,
            grupo_ref TEXT,
            tipo_item_ref TEXT
        );
        CREATE TABLE mercado_livre_invoices (
            id INTEGER PRIMARY KEY,
            numero_nfe TEXT,
            data_emissao TEXT,
            valor_total REAL,
            valor_produtos REAL,
            valor_frete REAL,
            tipo_logistico TEXT
        );
    """)
    conn.execute("INSERT INTO vendors (id, nome, tipo) VALUES (1, 'Vendedor Interno', 'INTERNO')")
    # Item normal: R$ 100,00, frete R$ 20,00
    conn.execute("""
        INSERT INTO invoices (numero, data_emissao, vendor_id, valor, valor_frete, quantidade, cliente_id, grupo_ref, tipo_item_ref)
        VALUES ('NF100', '2026-10-05', 1, 100.0, 20.0, 10, 1, 'SACOLAS', 'MERCADORIA')
    """)
    # Item clichê: R$ 50,00, frete R$ 0,00
    conn.execute("""
        INSERT INTO invoices (numero, data_emissao, vendor_id, valor, valor_frete, quantidade, cliente_id, grupo_ref, tipo_item_ref)
        VALUES ('NF101', '2026-10-06', 1, 50.0, 0.0, 1, 1, 'CLICHES', 'CLICHE')
    """)
    # Mercado Livre: 2 notas somando R$ 200,00
    conn.execute("INSERT INTO mercado_livre_invoices (numero_nfe, data_emissao, valor_total) VALUES ('ML1', '2026-10-15', 120.0)")
    conn.execute("INSERT INTO mercado_livre_invoices (numero_nfe, data_emissao, valor_total) VALUES ('ML2', '2026-10-20', 80.0)")
    conn.commit()

    d0 = date(2026, 10, 1)
    d1 = date(2026, 10, 31)

    # 1. Padrão: sem frete/clichê e sem Mercado Livre
    val_padrao, qtd_padrao, cli_padrao, unid_padrao, itens_padrao = m._soma_faturamento_periodo(
        conn, d0, d1, mostrar_frete_cliche=False, mostrar_ml=False
    )
    assert val_padrao == 100.0
    assert qtd_padrao == 1
    assert itens_padrao == 1

    # 2. Com frete/clichê: R$ 100 + R$ 20 (frete) + R$ 50 (clichê) = R$ 170
    val_fc, qtd_fc, cli_fc, unid_fc, itens_fc = m._soma_faturamento_periodo(
        conn, d0, d1, mostrar_frete_cliche=True, mostrar_ml=False
    )
    assert val_fc == 170.0
    assert qtd_fc == 2
    assert itens_fc == 2

    # 3. Com Mercado Livre: R$ 100 + R$ 200 (ML) = R$ 300
    val_ml, qtd_ml, cli_ml, unid_ml, itens_ml = m._soma_faturamento_periodo(
        conn, d0, d1, mostrar_frete_cliche=False, mostrar_ml=True
    )
    assert val_ml == 300.0
    assert qtd_ml == 3 # 1 nota comum + 2 notas ML
    assert itens_ml == 3

    # 4. Ambos ativos: R$ 170 + R$ 200 = R$ 370
    val_ambos, qtd_ambos, _, _, _ = m._soma_faturamento_periodo(
        conn, d0, d1, mostrar_frete_cliche=True, mostrar_ml=True
    )
    assert val_ambos == 370.0

    # 5. _fat_por_equipe inclui Mercado Livre e Frete/Clichê quando ativos
    eq_padrao = m._fat_por_equipe(conn, d0, d1, mostrar_frete_cliche=False, mostrar_ml=False)
    assert eq_padrao["INTERNO"] == 100.0
    assert "MERCADO LIVRE" not in eq_padrao

    eq_fc = m._fat_por_equipe(conn, d0, d1, mostrar_frete_cliche=True, mostrar_ml=False)
    assert eq_fc["INTERNO"] == 170.0

    eq_ml = m._fat_por_equipe(conn, d0, d1, mostrar_frete_cliche=False, mostrar_ml=True)
    assert eq_ml["INTERNO"] == 100.0
    assert eq_ml["MERCADO LIVRE"] == 200.0


def test_devolucao_mercado_livre_desconta_notas_e_quantidade_no_desempenho() -> None:
    import sqlite3
    from datetime import date
    from crm_npd import legacy_monolith as m

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE customers (id INTEGER PRIMARY KEY, nome TEXT);
        CREATE TABLE vendors (id INTEGER PRIMARY KEY, nome TEXT, tipo TEXT);
        CREATE TABLE invoices (
            id INTEGER PRIMARY KEY,
            numero TEXT,
            data_emissao TEXT,
            vendor_id INTEGER,
            valor REAL,
            quantidade REAL,
            cliente_id INTEGER,
            tipo_item_ref TEXT
        );
        CREATE TABLE mercado_livre_invoices (
            id INTEGER PRIMARY KEY,
            data_emissao TEXT,
            valor_total REAL,
            valor_produtos REAL,
            valor_frete REAL,
            quantidade REAL,
            tipo_logistico TEXT
        );
    """)
    conn.executemany(
        "INSERT INTO mercado_livre_invoices (data_emissao, valor_total, quantidade) VALUES ('2026-10-05', ?, ?)",
        [(100.0, 10.0), (80.0, 8.0), (-30.0, 3.0)],
    )
    conn.commit()

    valor, notas, _clientes, unidades, itens = m._soma_faturamento_periodo(
        conn, date(2026, 10, 1), date(2026, 10, 31), mostrar_ml=True
    )

    assert valor == 150.0
    assert notas == 1
    assert unidades == 15.0
    assert itens == 1





@pytest.mark.parametrize('alias', ['i', 'o'])
def test_multiplos_papeis_combinam_com_outros_filtros(alias):
    import sqlite3
    from crm_npd import legacy_monolith as m

    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE itens (papel_ref TEXT, grupo_ref TEXT, valor REAL)')
    conn.executemany('INSERT INTO itens VALUES (?, ?, ?)', [
        ('KRAFT', 'SACOLAS', 10), ('BRANCO', 'SACOLAS', 20),
        ('SEDA', 'SACOLAS', 40), ('KRAFT', 'CAIXAS', 80),
    ])
    sql, params = m._desempenho_filtro_sql({'papel': ['KRAFT', 'BRANCO'], 'grupo': ['SACOLAS']}, alias)
    assert conn.execute(f'SELECT SUM(valor) FROM itens {alias} WHERE 1=1' + sql, params).fetchone()[0] == 30
    conn.close()


def test_multiplas_selecoes_preservadas_nos_links(admin_client):
    from html import unescape
    from urllib.parse import parse_qs, urlsplit
    import re

    response = admin_client.get('/desempenho?papel=KRAFT&papel=BRANCO&uf=SC&uf=PR')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'name="papel" multiple' in html
    assert 'value="KRAFT" selected' in html
    assert 'value="BRANCO" selected' in html
    links = [parse_qs(urlsplit(unescape(url)).query) for url in re.findall(r'href="([^"]+)"', html)]
    assert any(link.get('visao') == ['pedidos'] and link.get('papel') == ['KRAFT', 'BRANCO'] and link.get('uf') == ['SC', 'PR'] for link in links)


def test_filtros_abrem_vazios_e_com_componente_pesquisavel(admin_client):
    import re

    response = admin_client.get('/desempenho')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    selects = re.findall(
        r'<select class="form-select desempenho-multi".*?</select>',
        html,
        flags=re.DOTALL,
    )
    assert len(selects) == 7
    assert all(' selected' not in select for select in selects)
    assert '/static/js/desempenho-filtros.js?v=3' in html

    script = admin_client.get('/static/js/desempenho-filtros.js?v=3')
    assert script.status_code == 200
    source = script.get_data(as_text=True)
    assert "Digite para buscar" in source
    assert "desempenho-multi-tag" in source
