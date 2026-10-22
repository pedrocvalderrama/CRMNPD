"""Periodos e filtros da central de relatorios (portados do CRM 10.10)."""

from __future__ import annotations

from datetime import date

from crm_npd.services.relatorios import (
    _report_apply_dataset_filters,
    _report_months_label,
    _report_selected_months,
    _resolve_report_period,
)


class _Args(dict):
    def getlist(self, key):
        val = self.get(key)
        if val is None:
            return []
        if isinstance(val, list):
            return val
        return [val]


def test_resolve_mes_calendario() -> None:
    d0, d1, label = _resolve_report_period("mes", date(2026, 9, 10))
    assert d0 == date(2026, 9, 1)
    assert d1 == date(2026, 9, 30)
    assert "2026" in label


def test_resolve_trimestre() -> None:
    d0, d1, label = _resolve_report_period("trimestre", date(2026, 8, 1))
    assert d0 == date(2026, 7, 1)
    assert d1 == date(2026, 9, 30)
    assert "3" in label


def test_meses_selecionados_estilo_excel() -> None:
    meses = _report_selected_months(_Args(mes=["10", "11"], meses="12"))
    assert meses == [10, 11, 12]
    assert "Out" in _report_months_label([10], 2026) or "out" in _report_months_label([10], 2026).lower()


def test_filtro_dataset_por_vendedor() -> None:
    ds = {
        "orders": [
            {"data": "2026-09-01", "pedido": "1", "vendedor": "Ana", "cliente": "X", "grupo": "", "papel": "", "medida": "", "valor": 10, "quantidade": 1},
            {"data": "2026-09-02", "pedido": "2", "vendedor": "Bruno", "cliente": "Y", "grupo": "", "papel": "", "medida": "", "valor": 20, "quantidade": 1},
        ],
        "invoices": [],
    }
    out = _report_apply_dataset_filters(ds, vendedor="Ana")
    assert out["p_orders"] == 1
    assert out["p_value"] == 10.0
