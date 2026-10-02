"""Regressao golden-master: a refatoracao nao pode mudar numero sem aprovacao.

Este teste e a rede de seguranca central do projeto. Ele recaptura todos os
calculos do sistema sobre a base congelada e compara com o baseline versionado
em ``tests/golden/baseline/golden.json``.

Quando falhar, ha duas leituras possiveis:

1. A refatoracao introduziu um bug. Corrija o codigo.
2. A mudanca de numero era intencional e foi aprovada. Nesse caso regrave o
   baseline com ``python scripts/golden_capture.py --baseline`` e descreva no
   commit exatamente qual regra mudou e por que.

Nunca regrave o baseline para "fazer o teste passar".
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BASELINE = PROJECT_ROOT / "tests" / "golden" / "baseline" / "golden.json"
SAIDA = PROJECT_ROOT / "tests" / "golden" / "output" / "golden.json"
FIXTURE = PROJECT_ROOT / "tests" / "golden" / "fixture" / "baseline.db"

# Limite de divergencias listadas na mensagem de falha: o suficiente para
# diagnosticar sem despejar milhares de linhas no terminal.
MAX_DIVERGENCIAS_EXIBIDAS = 40


def _achatar(obj: Any, prefixo: str = "") -> dict[str, Any]:
    """Transforma a arvore JSON em um mapa caminho -> valor escalar.

    Comparar folha por folha produz mensagens de erro que apontam exatamente
    qual vendedor, qual ciclo e qual metrica mudou.

    Args:
        obj: Estrutura carregada do JSON.
        prefixo: Caminho acumulado.

    Returns:
        Mapa de caminho para valor escalar.
    """
    plano: dict[str, Any] = {}
    if isinstance(obj, dict):
        for chave, valor in obj.items():
            plano.update(_achatar(valor, f"{prefixo}.{chave}" if prefixo else str(chave)))
    elif isinstance(obj, list):
        for indice, valor in enumerate(obj):
            plano.update(_achatar(valor, f"{prefixo}[{indice}]"))
    else:
        plano[prefixo] = obj
    return plano


@pytest.fixture(scope="module")
def captura_atual() -> dict[str, Any]:
    """Roda a captura no estado atual do codigo e devolve o JSON produzido."""
    if not FIXTURE.exists() and not (PROJECT_ROOT / "crm_npd.db").exists():
        pytest.skip("Base de dados indisponivel: golden master exige crm_npd.db")

    resultado = subprocess.run(  # noqa: S603
        [sys.executable, str(PROJECT_ROOT / "scripts" / "golden_capture.py")],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert resultado.returncode == 0, (
        f"A captura golden-master falhou:\n{resultado.stdout[-3000:]}\n{resultado.stderr[-3000:]}"
    )
    return json.loads(SAIDA.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def baseline() -> dict[str, Any]:
    """Carrega o baseline aprovado."""
    if not BASELINE.exists():
        pytest.skip(
            "Baseline ausente. Gere com: python scripts/golden_capture.py --baseline"
        )
    return json.loads(BASELINE.read_text(encoding="utf-8"))


@pytest.mark.golden
def test_fixture_nao_mudou(captura_atual: dict[str, Any], baseline: dict[str, Any]) -> None:
    """A base congelada tem de ser a mesma, senao a comparacao nao vale nada."""
    esperado = baseline["__meta__"]["fixture_sha256"]
    obtido = captura_atual["__meta__"]["fixture_sha256"]
    assert obtido == esperado, (
        "A fixture tests/golden/fixture/baseline.db mudou desde a gravacao do "
        "baseline. Qualquer diferenca de numero a partir daqui e inconclusiva. "
        "Restaure a fixture original ou regrave o baseline de forma consciente."
    )


@pytest.mark.golden
def test_sem_novos_erros(captura_atual: dict[str, Any], baseline: dict[str, Any]) -> None:
    """Nenhum calculo que funcionava pode passar a lancar excecao."""
    antes = {c for c, v in _achatar(baseline).items() if c.endswith("__erro__")}
    agora = {c for c, v in _achatar(captura_atual).items() if c.endswith("__erro__")}
    novos = sorted(agora - antes)
    assert not novos, (
        f"{len(novos)} calculo(s) passaram a falhar depois da refatoracao:\n"
        + "\n".join(f"  {c}" for c in novos[:MAX_DIVERGENCIAS_EXIBIDAS])
    )


@pytest.mark.golden
def test_numeros_identicos(captura_atual: dict[str, Any], baseline: dict[str, Any]) -> None:
    """Todo valor calculado deve bater com o baseline aprovado."""
    esperado = _achatar(baseline)
    obtido = _achatar(captura_atual)
    esperado.pop("__meta__.fixture_sha256", None)
    obtido.pop("__meta__.fixture_sha256", None)

    faltando = sorted(set(esperado) - set(obtido))
    sobrando = sorted(set(obtido) - set(esperado))
    diferentes = [
        (c, esperado[c], obtido[c])
        for c in sorted(set(esperado) & set(obtido))
        if esperado[c] != obtido[c]
    ]

    if not (faltando or sobrando or diferentes):
        return

    partes: list[str] = []
    if diferentes:
        partes.append(f"{len(diferentes)} valor(es) mudaram:")
        for caminho, antes, agora in diferentes[:MAX_DIVERGENCIAS_EXIBIDAS]:
            partes.append(f"  {caminho}\n    baseline={antes!r}\n    atual   ={agora!r}")
    if faltando:
        partes.append(f"{len(faltando)} valor(es) desapareceram:")
        partes += [f"  {c}" for c in faltando[:MAX_DIVERGENCIAS_EXIBIDAS]]
    if sobrando:
        partes.append(f"{len(sobrando)} valor(es) novos (nao e erro por si so):")
        partes += [f"  {c}" for c in sobrando[:MAX_DIVERGENCIAS_EXIBIDAS]]
    pytest.fail("\n".join(partes), pytrace=False)
