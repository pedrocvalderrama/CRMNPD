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
