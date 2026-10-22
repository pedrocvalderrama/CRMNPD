"""Testes das formulas de comissao.

Cobre o caso tipico, os degraus de bonus, divisao por zero e valores nulos.
Qualquer mudanca aqui e mudanca de dinheiro pago.
"""

from __future__ import annotations

import pytest

from crm_npd.domain.enums import StatusManualCliente
from crm_npd.domain.valores import LinhaComissao
from crm_npd.metrics import comissao as c


def test_linha_interno_recorrente() -> None:
    base, bonus, valor = c.comissao_linha_interno(10_000.0, is_novo=False)
    assert base == 1.0
    assert bonus == 0.0
    assert valor == 100.0


def test_linha_interno_cliente_novo() -> None:
    base, bonus, valor = c.comissao_linha_interno(10_000.0, is_novo=True)
    assert base == 1.0
    assert bonus == 1.0
    assert valor == 200.0


def test_linha_interno_apos_meta() -> None:
    base, bonus, valor = c.comissao_linha_interno(10_000.0, is_novo=False, bateu_meta=True)
    assert base == 1.75
    assert bonus == 0.0
    assert valor == pytest.approx(175.0)


def test_linha_interno_novo_apos_meta_continua_dois_por_cento() -> None:
    base, bonus, valor = c.comissao_linha_interno(10_000.0, is_novo=True, bateu_meta=True)
    assert base == 1.75
    assert bonus == 0.25
    assert valor == 200.0


def test_linha_interno_zero() -> None:
    assert c.comissao_linha_interno(0.0, is_novo=True)[2] == 0.0


def test_bonus_volume_abaixo_no_limite_e_acima() -> None:
    assert c.bonus_volume(99_999.99) == 0.0
    assert c.bonus_volume(100_000.0) == 0.0
    assert c.bonus_volume(150_000.0) == 500.0
    assert c.bonus_volume(0) == 0.0
    assert c.bonus_volume(None) == 0.0  # type: ignore[arg-type]


def test_bonus_volume_cai_quando_bate_meta() -> None:
    assert c.bonus_volume(150_000.0, bateu_meta=True) == 0.0
    assert c.bonus_volume(150_000.0, bateu_meta=False) == 500.0


def test_bateu_meta_e_faturamento_do_ciclo() -> None:
    assert c.bateu_meta_faturamento(100_000.0, 100_000.0) is True
    assert c.bateu_meta_faturamento(99_999.99, 100_000.0) is False
    assert c.bateu_meta_faturamento(150_000.0, 0.0) is False
    assert c.bateu_meta_faturamento(150_000.0, None) is False  # type: ignore[arg-type]


def test_externo_sete_por_cento() -> None:
    assert c.comissao_linha_externo(1_000.0) == 70.0
    assert c.comissao_linha_externo(0) == 0.0


def test_pedido_sisplan_primeira_compra() -> None:
    assert c.pedido_sisplan_cliente_novo("10 - PEDIDO PRIMEIRA COMPRA") is True
    assert c.pedido_sisplan_cliente_novo("10", "PEDIDO PRIMEIRA COMPRA") is True
    assert c.pedido_sisplan_cliente_novo("01 - ARTE REPETICAO") is False
    assert c.pedido_sisplan_cliente_novo("00 - ARTE NOVA") is False
    assert c.pedido_sisplan_cliente_novo("11 - PEDIDO RECUPERADO ARTE NOVA") is False
    assert c.pedido_sisplan_cliente_novo("") is False


def test_todas_as_notas_do_pedido_novo_ganham_bonus() -> None:
    assert c.classificar_cliente_novo(pedido_sisplan_novo=True, is_jit_imediato=False) is True
    assert c.classificar_cliente_novo(pedido_sisplan_novo=True, is_jit_imediato=False) is True


def test_jit_imediato_anula_mesmo_sisplan_e_override() -> None:
    assert (
        c.classificar_cliente_novo(
            pedido_sisplan_novo=True,
            is_jit_imediato=True,
            status_manual=StatusManualCliente.NOVO,
        )
        is False
    )
    assert c.condicao_jit_imediato("05 - JIT IMEDIATO") is True
    assert c.condicao_jit_imediato("98 - JIT") is False
    assert c.condicao_jit_imediato("04 - JIT") is False


def test_override_manual_e_jit_imediato_vence() -> None:
    assert (
        c.classificar_cliente_novo(
            pedido_sisplan_novo=True,
            is_jit_imediato=False,
            status_manual=StatusManualCliente.RECOMPRA,
        )
        is False
    )
    assert (
        c.classificar_cliente_novo(
            pedido_sisplan_novo=False,
            is_jit_imediato=False,
            status_manual=StatusManualCliente.NOVO,
        )
        is True
    )


def test_devolucao_nao_entra_na_base() -> None:
    assert c.invoice_eh_devolucao(tipo_item_ref="D", valor=-500.0) is True
    assert c.invoice_eh_devolucao(tipo_item_ref="V", valor=100.0) is False
    assert c.invoice_eh_devolucao(tipo_item_ref="DEMO", valor=80.0) is False
    assert c.invoice_eh_devolucao(tipo_item_ref="V", valor=-10.0) is True


def test_extrato_interno_soma_bonus_uma_vez() -> None:
    linhas = (
        LinhaComissao(data="2026-09-01", cliente="A", valor=80_000.0, is_novo=False, comissao=800.0, base_pct=1.0, bonus_pct=0.0),
        LinhaComissao(data="2026-09-02", cliente="B", valor=40_000.0, is_novo=True, comissao=800.0, base_pct=1.0, bonus_pct=1.0),
    )
    extrato = c.montar_extrato_interno(linhas)
    assert extrato.faturamento == 120_000.0
    assert extrato.extra_bonus == 200.0
    assert extrato.comissao == 1_800.0
    assert extrato.novo_count == 1
    assert extrato.rep_count == 1
    assert extrato.bateu_meta is False


def test_extrato_interno_meta_batida_sem_bonus_volume() -> None:
    linhas = (
        LinhaComissao(data="2026-09-01", cliente="A", valor=80_000.0, is_novo=False, comissao=1_400.0, base_pct=1.75, bonus_pct=0.0),
        LinhaComissao(data="2026-09-02", cliente="B", valor=40_000.0, is_novo=True, comissao=800.0, base_pct=1.75, bonus_pct=0.25),
    )
    extrato = c.montar_extrato_interno(linhas, bateu_meta=True)
    assert extrato.faturamento == 120_000.0
    assert extrato.extra_bonus == 0.0
    assert extrato.comissao == 2_200.0
    assert extrato.bateu_meta is True


def test_extrato_interno_devolucao_nao_abate() -> None:
    linhas = (
        LinhaComissao(data="2026-09-01", cliente="A", valor=10_000.0, is_novo=False, comissao=100.0, base_pct=1.0, bonus_pct=0.0),
        LinhaComissao(
            data="2026-09-02",
            cliente="A",
            valor=-2_000.0,
            is_novo=False,
            comissao=0.0,
            base_pct=0.0,
            bonus_pct=0.0,
            is_devolucao=True,
        ),
    )
    extrato = c.montar_extrato_interno(linhas)
    assert extrato.faturamento == 10_000.0
    assert extrato.comissao == 100.0
    assert extrato.devolucoes_valor == -2_000.0
    assert extrato.rep_count == 1
    assert len(extrato.lines) == 2


def test_linhas_excel_extrato_inclui_bonus_e_devolucao() -> None:
    from crm_npd.services.relatorios import linhas_excel_extrato_comissao

    data_fin = {
        "extra_bonus": 200.0,
        "lines": [
            {
                "data": "2026-09-01",
                "doc_ref": "NF-1",
                "pedido": "100",
                "cliente": "A",
                "identity_nome": "A",
                "is_novo": False,
                "is_jit": False,
                "is_devolucao": False,
                "is_anfarma": True,
                "valor": 80_000.0,
                "base_pct": 1.0,
                "bonus_pct": 0.0,
                "comissao": 800.0,
            },
            {
                "data": "2026-09-02",
                "doc_ref": "NF-2",
                "pedido": "101",
                "cliente": "B",
                "identity_nome": "B",
                "is_novo": True,
                "is_jit": False,
                "is_devolucao": False,
                "valor": 40_000.0,
                "base_pct": 1.0,
                "bonus_pct": 1.0,
                "comissao": 800.0,
            },
            {
                "data": "2026-09-03",
                "doc_ref": "E-9",
                "pedido": "102",
                "cliente": "A",
                "identity_nome": "A",
                "is_novo": False,
                "is_jit": False,
                "is_devolucao": True,
                "valor": -500.0,
                "base_pct": 0.0,
                "bonus_pct": 0.0,
                "comissao": 0.0,
            },
        ],
    }
    rows = linhas_excel_extrato_comissao(data_fin)
    assert rows[0][5] == "CARTEIRA"
    assert rows[0][8] == pytest.approx(0.01)
    assert rows[0][10] == "SIM"
    assert rows[1][5] == "NOVO"
    assert rows[1][8] == pytest.approx(0.02)
    assert rows[2][5] == "DEVOLUÇÃO"
    assert rows[2][8] is None
    assert rows[-1][3].startswith("Bônus")
    assert rows[-1][9] == 200.0
