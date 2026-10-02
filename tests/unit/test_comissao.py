"""Testes das formulas de comissao.

Cobre o caso tipico, os degraus de bonus, divisao por zero e valores nulos.
Qualquer mudanca aqui e mudanca de dinheiro pago.
"""

from __future__ import annotations

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


def test_linha_interno_zero() -> None:
    assert c.comissao_linha_interno(0.0, is_novo=True)[2] == 0.0


def test_bonus_volume_abaixo_no_limite_e_acima() -> None:
    assert c.bonus_volume(99_999.99) == 0.0
    assert c.bonus_volume(100_000.0) == 0.0
    assert c.bonus_volume(150_000.0) == 500.0
    assert c.bonus_volume(0) == 0.0
    assert c.bonus_volume(None) == 0.0  # type: ignore[arg-type]


def test_externo_sete_por_cento() -> None:
    assert c.comissao_linha_externo(1_000.0) == 70.0
    assert c.comissao_linha_externo(0) == 0.0


def test_cliente_novo_somente_primeiro_documento() -> None:
    kwargs = dict(
        primeiro_documento="NF-1",
        primeira_data_iso="2026-09-01",
        periodo_inicio_iso="2026-08-25",
        periodo_fim_iso="2026-09-24",
        is_jit=False,
    )
    assert c.classificar_cliente_novo(documento_atual="NF-1", **kwargs) is True
    assert c.classificar_cliente_novo(documento_atual="NF-2", **kwargs) is False


def test_cliente_novo_fora_do_ciclo() -> None:
    assert (
        c.classificar_cliente_novo(
            primeiro_documento="NF-1",
            primeira_data_iso="2026-07-01",
            documento_atual="NF-1",
            periodo_inicio_iso="2026-08-25",
            periodo_fim_iso="2026-09-24",
            is_jit=False,
        )
        is False
    )


def test_override_manual_e_jit_vence() -> None:
    base = dict(
        primeiro_documento="NF-1",
        primeira_data_iso="2026-09-01",
        documento_atual="NF-1",
        periodo_inicio_iso="2026-08-25",
        periodo_fim_iso="2026-09-24",
    )
    assert c.classificar_cliente_novo(is_jit=False, status_manual=StatusManualCliente.RECOMPRA, **base) is False
    assert c.classificar_cliente_novo(is_jit=False, status_manual=StatusManualCliente.NOVO, **base) is True
    assert c.classificar_cliente_novo(is_jit=True, status_manual=StatusManualCliente.NOVO, **base) is False


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
