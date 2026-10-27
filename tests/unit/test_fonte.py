"""Modo local SQLite nao deve abrir o Sisplan."""

from crm_npd.sync.cdc import iniciar_cdc_em_background
from crm_npd.sync.fonte import FONTES_VALIDAS, fonte_primaria, usando_firebird


def test_padrao_e_firebird(monkeypatch) -> None:
    monkeypatch.delenv("CRM_NPD_FONTE", raising=False)
    assert fonte_primaria() == "firebird"
    assert usando_firebird() is True


def test_fonte_sqlite_desliga_sisplan(monkeypatch) -> None:
    monkeypatch.setenv("CRM_NPD_FONTE", "sqlite")
    assert fonte_primaria() == "sqlite"
    assert usando_firebird() is False
    assert "sqlite" in FONTES_VALIDAS


def test_cdc_nao_sobe_em_modo_sqlite(monkeypatch) -> None:
    monkeypatch.setenv("CRM_NPD_FONTE", "sqlite")
    monkeypatch.setenv("SISPLAN_CDC_AUTO", "1")
    assert iniciar_cdc_em_background() is False
