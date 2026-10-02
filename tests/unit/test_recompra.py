"""Testes de recompra, atraso e cliente recuperado."""

from __future__ import annotations

from datetime import date

from crm_npd.domain.enums import ClassificacaoCliente
from crm_npd.metrics import recompra as r


def test_datas_unicas_remove_repeticao_do_mesmo_dia() -> None:
    d = date(2026, 9, 1)
    assert r.datas_unicas_ordenadas([d, d, date(2026, 8, 1)]) == [date(2026, 8, 1), d]


def test_intervalo_mediano_resiste_a_outlier() -> None:
    datas = [date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1), date(2025, 10, 1)]
    assert r.intervalo_mediano(datas) == 31
    media = r.intervalo_medio(datas)
    assert media is not None and media > 200


def test_intervalo_sem_historico() -> None:
    assert r.intervalo_mediano([date(2026, 1, 1)]) is None
    assert r.intervalo_medio([]) is None
    assert r.atraso_atual(None, date(2026, 10, 1)) is None


def test_atraso_e_situacao() -> None:
    assert r.atraso_atual(date(2026, 9, 1), date(2026, 10, 1)) == 30
    assert r.classificar_situacao(None, None) is ClassificacaoCliente.NOVO
    assert r.classificar_situacao(10, 30.0) is ClassificacaoCliente.ATIVO
    assert r.classificar_situacao(35, 30.0) is ClassificacaoCliente.EM_RISCO
    assert r.classificar_situacao(50, 30.0) is ClassificacaoCliente.INATIVO


def test_esta_atrasado() -> None:
    assert r.esta_atrasado(31, 30.0) is True
    assert r.esta_atrasado(30, 30.0) is False
    assert r.esta_atrasado(None, 30.0) is False
    assert r.esta_atrasado(10, None) is False


def test_recuperado_exige_duas_compras_anteriores() -> None:
    hist = [date(2026, 1, 1), date(2026, 2, 1)]
    assert r.e_cliente_recuperado(date(2026, 6, 1), hist) is True
    assert r.e_cliente_recuperado(date(2026, 2, 10), hist) is False
    assert r.e_cliente_recuperado(date(2026, 6, 1), [date(2026, 1, 1)]) is False
