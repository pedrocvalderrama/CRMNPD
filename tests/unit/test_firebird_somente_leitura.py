"""Garante que o conector Firebird recusa qualquer SQL que nao seja SELECT."""

from __future__ import annotations

import pytest

from crm_npd.repositories.firebird.conexao import (
    FirebirdSomenteLeituraError,
    _kwargs_conexao,
    _modo_local,
    _validar_select,
    descricao_conexao,
)


def test_select_simples_passa() -> None:
    assert _validar_select("SELECT 1 FROM RDB$DATABASE").startswith("SELECT")


def test_select_com_espaco_passa() -> None:
    assert _validar_select("  select fatura from nota_001  ")


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO NOTA_001 (FATURA) VALUES (1)",
        "UPDATE NOTA_001 SET SITUACAO='X'",
        "DELETE FROM NOTA_001",
        "DROP TABLE NOTA_001",
        "ALTER TABLE NOTA_001 ADD X INT",
        "CREATE TABLE X (ID INT)",
        "TRUNCATE TABLE NOTA_001",
        "EXECUTE PROCEDURE FOO",
        "SELECT 1; DELETE FROM NOTA_001",
    ],
)
def test_escrita_e_bloqueada(sql: str) -> None:
    with pytest.raises(FirebirdSomenteLeituraError):
        _validar_select(sql)


def test_conexao_fabrica_usa_host_porta_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SISPLAN_HOST", "192.168.100.168")
    monkeypatch.setenv("SISPLAN_PORT", "3051")
    monkeypatch.setenv("SISPLAN_DATABASE", r"C:\Sisplan\Base\14215F.FDB")
    monkeypatch.setenv("SISPLAN_USER", "SYSDBA")
    monkeypatch.setenv("SISPLAN_PASSWORD", "masterkey")
    monkeypatch.setenv("SISPLAN_CHARSET", "WIN1252")
    kwargs = _kwargs_conexao()
    assert kwargs["host"] == "192.168.100.168"
    assert kwargs["port"] == 3051
    assert kwargs["database"] == r"C:\Sisplan\Base\14215F.FDB"
    assert kwargs["user"] == "SYSDBA"
    assert "dsn" not in kwargs
    assert not _modo_local()
    assert "192.168.100.168:3051" in descricao_conexao()


def test_fbclient_busca_x64_do_manual(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SISPLAN_FBCLIENT", raising=False)
    from crm_npd.repositories.firebird.conexao import _candidatos_fbclient

    caminhos = [str(p).lower() for p in _candidatos_fbclient()]
    assert any(c.endswith("fbclient_x64.dll") for c in caminhos)
    assert any("auto_sisplan" in c and c.endswith("fbclient_x64.dll") for c in caminhos)


def test_conexao_local_usa_dsn(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    caminho = tmp_path / "14215F.FDB"
    monkeypatch.setenv("SISPLAN_HOST", "local")
    monkeypatch.setenv("SISPLAN_DATABASE", str(caminho))
    monkeypatch.setenv("SISPLAN_PASSWORD", "masterkey")
    kwargs = _kwargs_conexao()
    assert kwargs["dsn"] == str(caminho)
    assert "host" not in kwargs
    assert _modo_local()
