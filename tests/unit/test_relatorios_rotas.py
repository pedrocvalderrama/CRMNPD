"""Smoke das rotas portadas do CRM 10.10."""

from __future__ import annotations

from crm_npd.bootstrap import create_app


def test_paginas_novas_existem_e_abrem_com_admin() -> None:
    app = create_app()
    regras = {r.rule for r in app.url_map.iter_rules()}
    for caminho in (
        "/relatorios",
        "/relatorios/exportar",
        "/relatorios/favoritos",
        "/a-faturar",
        "/dashboard/detalhe-pedidos",
        "/analises/historico",
        "/vendedores/inativos",
        "/vendedores/acao/<int:vendor_id>",
        "/vendedor/<int:vendor_id>/comissao.xlsx",
    ):
        assert caminho in regras

    app.config["TESTING"] = True
    client = app.test_client()
    login = client.post(
        "/login",
        data={"email": "admin@npd.local", "senha": "npdadmin"},
        follow_redirects=False,
    )
    assert login.status_code in (302, 303)
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
