"""Smoke das rotas portadas do CRM 10.10."""

from __future__ import annotations

from io import BytesIO

import pytest
from openpyxl import load_workbook

from crm_npd.bootstrap import create_app


@pytest.fixture(scope="module")
def admin_app_client():
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    login = client.post(
        "/login",
        data={"email": "admin@npd.local", "senha": "npdadmin"},
        follow_redirects=False,
    )
    assert login.status_code in (302, 303)
    return app, client


def test_paginas_novas_existem_e_abrem_com_admin(admin_app_client) -> None:
    app, client = admin_app_client
    regras = {r.rule for r in app.url_map.iter_rules()}
    for caminho in (
        "/relatorios",
        "/relatorios/exportar",
        "/relatorios/favoritos",
        "/a-faturar",
        "/dashboard/detalhe-pedidos",
        "/dashboard/faturamento.xlsx",
        "/dashboard/pedidos.xlsx",
        "/analises/historico",
        "/vendedores/inativos",
        "/vendedores/acao/<int:vendor_id>",
        "/vendedor/<int:vendor_id>/comissao.xlsx",
    ):
        assert caminho in regras

    for caminho in (
        "/dashboard",
        "/relatorios",
        "/a-faturar",
        "/analises/historico",
        "/vendedores",
        "/vendedores/inativos",
        "/dashboard/detalhe-pedidos",
        "/clientes",
        "/tarefas",
    ):
        resp = client.get(caminho)
        assert resp.status_code == 200, f"{caminho} -> {resp.status_code}"
        html = resp.get_data(as_text=True)
        assert "nav-group" in html
        assert "Relatórios" in html


def test_dashboard_faturamento_e_pedidos_periodo_com_excel(admin_app_client) -> None:
    _app, client = admin_app_client

    resp_fat = client.get("/dashboard?visao=faturamento&data_ref=2026-10")
    assert resp_fat.status_code == 200
    html_fat = resp_fat.get_data(as_text=True)
    assert "SITE / MARKETPLACE" in html_fat
    assert "Interno, Externo e Site no ciclo financeiro." in html_fat
    assert "Faturamento Líquido" in html_fat
    assert "Faturamento Bruto" in html_fat
    assert "Valor Devolvido" in html_fat
    assert "Faturamento da Equipe" in html_fat
    assert "Faturamento do Período" in html_fat
    assert "/dashboard/faturamento.xlsx" in html_fat
    assert html_fat.index("Faturamento da Equipe") < html_fat.index("Faturamento do Período")

    resp_fat_custom = client.get(
        "/dashboard?visao=faturamento&data_ref=2026-10&fat_de=2026-10-05&fat_ate=2026-10-15"
    )
    assert resp_fat_custom.status_code == 200
    html_fat_custom = resp_fat_custom.get_data(as_text=True)
    assert "fat_de=2026-10-05" in html_fat_custom
    assert "fat_ate=2026-10-15" in html_fat_custom

    resp_ped = client.get("/dashboard?visao=pedidos&data_ref=2026-10")
    assert resp_ped.status_code == 200
    html_ped = resp_ped.get_data(as_text=True)
    assert "Pedidos do Período" in html_ped
    assert "/dashboard/pedidos.xlsx" in html_ped
    assert "Últimos Pedidos Tirados" not in html_ped

    xlsx_fat = client.get(
        "/dashboard/faturamento.xlsx?data_ref=2026-10&fat_de=2026-10-05&fat_ate=2026-10-15"
    )
    assert xlsx_fat.status_code == 200
    assert "spreadsheetml" in xlsx_fat.content_type
    wb_fat = load_workbook(BytesIO(xlsx_fat.data))
    assert wb_fat.sheetnames == ["Resumo", "Notas Fiscais", "Itens Faturados"]
    resumo_fat_text = " ".join(
        str(val)
        for row in wb_fat["Resumo"].iter_rows(values_only=True)
        for val in row
        if val is not None
    )
    assert "Valor Site" in resumo_fat_text
    assert "Faturamento Bruto" in resumo_fat_text
    assert "Valor Devolvido" in resumo_fat_text
    assert "Faturamento Líquido" in resumo_fat_text

    xlsx_ped = client.get(
        "/dashboard/pedidos.xlsx?data_ref=2026-10&ped_de=2026-10-05&ped_ate=2026-10-15"
    )
    assert xlsx_ped.status_code == 200
    assert "spreadsheetml" in xlsx_ped.content_type
    wb_ped = load_workbook(BytesIO(xlsx_ped.data))
    assert wb_ped.sheetnames == ["Resumo", "Pedidos", "Itens dos Pedidos"]
