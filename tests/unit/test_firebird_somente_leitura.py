"""Garante que o conector Firebird recusa qualquer SQL que nao seja SELECT."""

from __future__ import annotations

import pytest

from crm_npd.repositories.firebird.conexao import FirebirdSomenteLeituraError, _validar_select


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
