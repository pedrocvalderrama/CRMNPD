"""Testes unitarios da importacao e resumo de notas fiscais do Mercado Livre."""

from __future__ import annotations

import sqlite3
from datetime import date
from io import BytesIO

from openpyxl import Workbook
import pytest

from crm_npd.services.mercado_livre import (
    importar_planilha_mercado_livre,
    ler_planilha_mercado_livre,
    listar_faturamento_mercado_livre_periodo,
    resumo_mercado_livre_periodo,
)
from crm_npd.sync.mercado_livre_sheets import (
    intervalo_sync_ml,
    obter_status_sync_ml,
    sincronizar_planilha_ml_delta,
    sync_ml_auto_ativo,
    sync_ml_on_start_ativo,
    verificar_planilha_ml_na_subida,
)


def _gerar_xlsx_ml_exemplo() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Notas Fiscais Emitidas"
    ws.append(
        [
            "Status",
            "Número NF-e",
            "Série",
            "Data Emissão da NF-e",
            "Chave de Acesso (44 dígitos)",
            "Pedido ML",
            "Destinatário",
            "CPF / CNPJ",
            "Cidade",
            "UF",
            "Tipo Logístico",
            "Valor Produtos (R$)",
            "Frete (R$)",
            "Valor Total NF-e (R$)",
            "Itens",
        ]
    )
    ws.append(
        [
            "Autorizada",
            "34576",
            "3",
            "31/10/2026 14:04:18",
            "42261019871954000106550030000345761545974895",
            "2000019250448176",
            "Cliente Full",
            "04006638990",
            "Curitiba",
            "PR",
            "fulfillment",
            200.00,
            25.50,
            225.50,
            "SACOLA KRAFT (1x), CAIXA BOLO (2x)",
        ]
    )
    ws.append(
        [
            "Autorizada",
            "34570",
            "3",
            "15/10/2026 10:00:00",
            "42261019871954000106550030000345701345202690",
            "2000019247149382",
            "Cliente Envios",
            "03647780677",
            "São Gotardo",
            "MG",
            "xd_drop_off",
            100.00,
            0.00,
            100.00,
            "CAIXA TORTA (1x)",
        ]
    )
    ws.append(
        [
            "Cancelada",
            "34571",
            "3",
            "16/10/2026 10:00:00",
            "42261019871954000106550030000345719999999999",
            "2000019247149999",
            "Cliente Cancelado",
            "00000000000",
            "São Paulo",
            "SP",
            "fulfillment",
            500.00,
            10.00,
            510.00,
            "ITEM CANCELADO (1x)",
        ]
    )
    ws.append(
        [
            "TOTAL GERAL",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            300.00,
            25.50,
            325.50,
            None,
        ]
    )
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _criar_conn_memoria() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT,
            arquivo TEXT,
            imported_at TEXT,
            linhas INTEGER,
            registros INTEGER,
            novos INTEGER DEFAULT 0,
            substituidos INTEGER DEFAULT 0,
            duplicados_arquivo INTEGER DEFAULT 0
        )
        """
    )
    return conn


def test_importar_e_resumir_mercado_livre_com_frete_sem_duplicar() -> None:
    conteudo = _gerar_xlsx_ml_exemplo()
    linhas, regs = ler_planilha_mercado_livre(conteudo)
    assert linhas == 3  # ignora TOTAL GERAL
    assert len(regs) == 2  # ignora Cancelada
    assert regs[0]["valor_total"] == 225.50  # com frete!
    assert regs[0]["quantidade"] == 3.0  # (1x) + (2x)

    conn = _criar_conn_memoria()
    res1 = importar_planilha_mercado_livre(conn, "ml_outubro.xlsx", conteudo, "2026-11-03T15:30:00")
    assert res1["registros"] == 2
    assert res1["novos"] == 2
    assert res1["substituidos"] == 0
    assert res1["valor_total"] == 325.50

    # Reimportando o mesmo arquivo: atualiza pela Chave de Acesso sem duplicar
    res2 = importar_planilha_mercado_livre(conn, "ml_outubro.xlsx", conteudo, "2026-11-03T15:35:00")
    assert res2["registros"] == 2
    assert res2["novos"] == 0
    assert res2["substituidos"] == 2

    resumo = resumo_mercado_livre_periodo(conn, date(2026, 10, 1), date(2026, 10, 31))
    assert resumo["qtd_notas"] == 2
    assert resumo["qtd_full"] == 1
    assert resumo["qtd_envios"] == 1
    assert resumo["valor_produtos"] == 300.00
    assert resumo["valor_frete"] == 25.50
    assert resumo["valor_total"] == 325.50
    assert resumo["valor_bruto"] == 325.50
    assert resumo["valor_devolvido"] == 0.0

    nf_rows, item_rows = listar_faturamento_mercado_livre_periodo(
        conn, date(2026, 10, 1), date(2026, 10, 31), include_items=True
    )
    assert len(nf_rows) == 2
    assert len(item_rows) == 2
    assert nf_rows[0]["canal"] == "MERCADO LIVRE"
    assert nf_rows[0]["nf"] == "ML-34576"
    assert nf_rows[0]["valor"] == 225.50


def test_sync_incremental_planilha_sheets_por_timestamp_log() -> None:
    conn = _criar_conn_memoria()
    chamadas: list[tuple[str, str]] = []

    def fake_fetcher_inicial(url: str, since_ts: str) -> dict:
        chamadas.append((url, since_ts))
        return {
            "success": True,
            "has_changes": True,
            "last_ts": "2026-11-03 16:00:00",
            "last_row": 3,
            "total_notes_in_sheet": 2,
            "count": 2,
            "invoices": [
                {
                    "row_number": 2,
                    "timestamp_log": "2026-11-03 15:45:00",
                    "data_emissao": "03/11/2026 14:22:10",
                    "competencia": "11/2026",
                    "numero_nfe": "18452",
                    "serie": "1",
                    "status": "Autorizada",
                    "order_id": "2000009812345678",
                    "buyer_name": "JOAO DA SILVA",
                    "buyer_doc": "12345678901",
                    "cidade_uf": "São Paulo - SP",
                    "logistic_type": "fulfillment",
                    "valor_produtos": 149.90,
                    "frete": 18.50,
                    "valor_total": 168.40,
                    "itens": "Sacola Kraft M (2x)",
                    "chave": "35261119871954000106550010000184521000000001",
                },
                {
                    "row_number": 3,
                    "timestamp_log": "2026-11-03 16:00:00",
                    "data_emissao": "03/11/2026 15:50:00",
                    "competencia": "11/2026",
                    "numero_nfe": "18453",
                    "serie": "1",
                    "status": "Autorizada",
                    "order_id": "2000009812345679",
                    "buyer_name": "PEDRO SOUZA",
                    "buyer_doc": "11122233344",
                    "cidade_uf": "Joinville - SC",
                    "logistic_type": "xd_drop_off",
                    "valor_produtos": 80.00,
                    "frete": 12.00,
                    "valor_total": 92.00,
                    "itens": "Caixa Bolo (1x)",
                    "chave": "35261119871954000106550010000184531000000002",
                },
            ],
        }

    # 1) Primeira leitura (cold start: since_ts == "")
    res1 = sincronizar_planilha_ml_delta(conn, fetcher=fake_fetcher_inicial)
    assert chamadas[-1][1] == ""
    assert res1["has_changes"] is True
    assert res1["novos"] == 2
    assert res1["last_ts"] == "2026-11-03 16:00:00"
    assert res1["valor_total"] == 260.40

    row_sp = conn.execute(
        "SELECT cidade, uf, timestamp_log FROM mercado_livre_invoices WHERE numero_nfe = '18452'"
    ).fetchone()
    assert row_sp["cidade"] == "São Paulo"
    assert row_sp["uf"] == "SP"
    assert row_sp["timestamp_log"] == "2026-11-03 15:45:00"

    # 2) Leitura seguinte sem alterações (has_changes: false)
    def fake_fetcher_sem_mudanca(url: str, since_ts: str) -> dict:
        chamadas.append((url, since_ts))
        return {
            "success": True,
            "has_changes": False,
            "last_ts": "2026-11-03 16:00:00",
            "last_row": 3,
            "total_notes_in_sheet": 2,
            "count": 0,
            "invoices": [],
        }

    res2 = sincronizar_planilha_ml_delta(conn, fetcher=fake_fetcher_sem_mudanca)
    assert chamadas[-1][1] == "2026-11-03 16:00:00"
    assert res2["has_changes"] is False
    assert res2["novos"] == 0
    assert res2["substituidos"] == 0

    # 3) Novo delta após 15 min: entra NF 18460 e cancela NF 18453
    def fake_fetcher_delta(url: str, since_ts: str) -> dict:
        chamadas.append((url, since_ts))
        return {
            "success": True,
            "has_changes": True,
            "last_ts": "2026-11-03 16:30:12",
            "last_row": 5,
            "total_notes_in_sheet": 3,
            "count": 2,
            "invoices": [
                {
                    "row_number": 4,
                    "timestamp_log": "2026-11-03 16:29:50",
                    "data_emissao": "03/11/2026 15:50:00",
                    "numero_nfe": "18453",
                    "serie": "1",
                    "status": "Cancelada",
                    "chave": "35261119871954000106550010000184531000000002",
                },
                {
                    "row_number": 5,
                    "timestamp_log": "2026-11-03 16:30:12",
                    "data_emissao": "03/11/2026 16:21:04",
                    "competencia": "11/2026",
                    "numero_nfe": "18460",
                    "serie": "1",
                    "status": "Autorizada",
                    "order_id": "2000009899991111",
                    "buyer_name": "MARIA OLIVEIRA",
                    "buyer_doc": "98765432100",
                    "cidade_uf": "Curitiba - PR",
                    "logistic_type": "fulfillment",
                    "valor_produtos": 120.0,
                    "frete": 15.9,
                    "valor_total": 135.9,
                    "itens": "Sacola Personalizada (1x)",
                    "chave": "35261119871954000106550010000184601000000003",
                },
            ],
        }

    res3 = sincronizar_planilha_ml_delta(conn, fetcher=fake_fetcher_delta)
    assert chamadas[-1][1] == "2026-11-03 16:00:00"
    assert res3["novos"] == 1
    assert res3["removidos"] == 1
    assert res3["last_ts"] == "2026-11-03 16:30:12"

    resumo_nov = resumo_mercado_livre_periodo(conn, date(2026, 11, 1), date(2026, 11, 30))
    assert resumo_nov["qtd_notas"] == 2  # 18452 + 18460 (18453 removida por cancelamento)
    assert round(resumo_nov["valor_total"], 2) == 304.30  # 168.40 + 135.90

    # 4) Falha de rede não avança cursor e registra erro no status
    def fake_fetcher_falha(_url: str, _since_ts: str) -> dict:
        raise RuntimeError("timeout simulado")

    with pytest.raises(RuntimeError, match="timeout simulado"):
        sincronizar_planilha_ml_delta(conn, fetcher=fake_fetcher_falha)

    status = obter_status_sync_ml(conn)
    assert status["last_ts"] == "2026-11-03 16:30:12"
    assert status["last_status"].startswith("erro: timeout simulado")
    assert status["total_no_banco"] == 2

    # 5) forcar_completo=True envia since_ts="" mesmo com cursor salvo
    res_full = sincronizar_planilha_ml_delta(
        conn, forcar_completo=True, fetcher=fake_fetcher_sem_mudanca
    )
    assert chamadas[-1][1] == ""
    assert res_full["since_ts"] == ""


def test_configuracao_subida_e_intervalo_60s(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("ML_SHEETS_SYNC_ON_START", "1")
    monkeypatch.setenv("ML_SHEETS_SYNC_AUTO", "1")
    monkeypatch.setenv("ML_SHEETS_SYNC_INTERVAL", "60")

    assert sync_ml_on_start_ativo() is True
    assert sync_ml_auto_ativo() is True
    assert intervalo_sync_ml() == 60.0

    conn = _criar_conn_memoria()
    monkeypatch.setattr(
        "crm_npd.sync.mercado_livre_sheets.consultar_webhook_ml",
        lambda _url, _ts: {
            "success": True,
            "has_changes": True,
            "last_ts": "2026-11-03 16:40:00",
            "last_row": 2,
            "total_notes_in_sheet": 1,
            "count": 1,
            "invoices": [
                {
                    "timestamp_log": "2026-11-03 16:40:00",
                    "data_emissao": "03/11/2026 16:39:00",
                    "numero_nfe": "18500",
                    "serie": "1",
                    "status": "Autorizada",
                    "order_id": "2000009900000001",
                    "buyer_name": "CLIENTE SUBIDA",
                    "cidade_uf": "Blumenau - SC",
                    "logistic_type": "fulfillment",
                    "valor_produtos": 90.0,
                    "frete": 10.0,
                    "valor_total": 100.0,
                    "itens": "Sacola (1x)",
                    "chave": "35261119871954000106550010000185001000000099",
                }
            ],
        },
    )
    res_subida = verificar_planilha_ml_na_subida(conn)
    assert res_subida is not None
    assert res_subida["novos"] == 1
    assert res_subida["last_ts"] == "2026-11-03 16:40:00"
