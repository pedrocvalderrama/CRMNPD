"""Testes que protegem as fronteiras entre camadas.

A regra "formula nao fala com banco" esta configurada como lint em
``pyproject.toml`` (TID251), mas ruff e mypy sao binarios compilados e a politica
de Controle de Aplicativos do Windows pode bloquea-los nesta maquina. Um teste em
Python puro sobre a arvore sintatica roda em qualquer ambiente e torna a regra
executavel de verdade, nao apenas declarada.

Sem essa barreira, o historico do projeto se repete: o monolito chegou a ter
``periodo_ciclo_faturamento_manual`` abrindo conexao SQLite dentro de um calculo
de datas, o que tornou a regra de ciclo impossivel de testar isoladamente.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

#: Camadas puras: nao podem tocar infraestrutura.
CAMADAS_PURAS = ("crm_npd/metrics", "crm_npd/domain")

#: Modulos proibidos nas camadas puras, com o motivo exibido na falha.
IMPORTS_PROIBIDOS: dict[str, str] = {
    "sqlite3": "acesso a banco pertence a crm_npd/repositories",
    "fdb": "driver Firebird pertence a crm_npd/repositories/firebird",
    "flask": "camada web pertence a crm_npd/web",
    "werkzeug": "camada web pertence a crm_npd/web",
    "matplotlib": "geracao de grafico pertence a camada de apresentacao",
    "openai": "integracao externa pertence a crm_npd/services",
    "requests": "chamada de rede nao pertence a uma formula",
    "urllib": "chamada de rede nao pertence a uma formula",
    "os": "formula nao le ambiente: a configuracao entra por argumento",
}

#: Pacotes do proprio projeto que as camadas puras podem importar.
PACOTES_INTERNOS_PERMITIDOS = ("crm_npd.domain", "crm_npd.metrics")


def _modulos_puros() -> list[Path]:
    arquivos: list[Path] = []
    for camada in CAMADAS_PURAS:
        arquivos.extend(sorted((PROJECT_ROOT / camada).rglob("*.py")))
    return arquivos


def _raizes_importadas(arvore: ast.Module) -> set[str]:
    """Coleta a raiz de cada modulo importado no arquivo."""
    raizes: set[str] = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            for alias in no.names:
                raizes.add(alias.name.split(".")[0])
        elif isinstance(no, ast.ImportFrom):
            if no.level:  # import relativo: fica dentro da propria camada
                continue
            if no.module:
                raizes.add(no.module.split(".")[0])
    return raizes


def _modulos_completos_importados(arvore: ast.Module) -> set[str]:
    nomes: set[str] = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            nomes.update(alias.name for alias in no.names)
        elif isinstance(no, ast.ImportFrom) and no.module and not no.level:
            nomes.add(no.module)
    return nomes


def test_existem_modulos_puros_para_verificar() -> None:
    """Protege o proprio teste: se o glob parar de achar arquivo, ele falha."""
    assert len(_modulos_puros()) >= 8, (
        "Poucos modulos encontrados em metrics/domain. O teste de arquitetura "
        "estaria passando por nao verificar nada."
    )


@pytest.mark.parametrize("arquivo", _modulos_puros(), ids=lambda p: p.name)
def test_camada_pura_sem_infraestrutura(arquivo: Path) -> None:
    """Nenhum modulo de metrics/domain importa banco, web ou rede."""
    arvore = ast.parse(arquivo.read_text(encoding="utf-8"), filename=str(arquivo))
    encontrados = _raizes_importadas(arvore) & set(IMPORTS_PROIBIDOS)
    assert not encontrados, "\n".join(
        f"{arquivo.relative_to(PROJECT_ROOT)} importa '{nome}': {IMPORTS_PROIBIDOS[nome]}"
        for nome in sorted(encontrados)
    )


@pytest.mark.parametrize("arquivo", _modulos_puros(), ids=lambda p: p.name)
def test_camada_pura_nao_depende_de_camadas_superiores(arquivo: Path) -> None:
    """metrics/domain nao podem importar repositories, services, web, sync ou cli."""
    arvore = ast.parse(arquivo.read_text(encoding="utf-8"), filename=str(arquivo))
    internos = {
        nome
        for nome in _modulos_completos_importados(arvore)
        if nome.startswith("crm_npd")
    }
    invalidos = sorted(
        nome
        for nome in internos
        if not nome.startswith(PACOTES_INTERNOS_PERMITIDOS)
    )
    assert not invalidos, (
        f"{arquivo.relative_to(PROJECT_ROOT)} importa camada superior: {invalidos}. "
        "A dependencia tem de apontar para dentro: web -> services -> metrics."
    )


@pytest.mark.parametrize("arquivo", _modulos_puros(), ids=lambda p: p.name)
def test_dominio_nao_depende_de_metrics(arquivo: Path) -> None:
    """O dominio e a base: ele nao pode depender das formulas que o consomem."""
    if "domain" not in arquivo.parts:
        pytest.skip("aplica-se apenas a crm_npd/domain")
    arvore = ast.parse(arquivo.read_text(encoding="utf-8"), filename=str(arquivo))
    assert not any(
        nome.startswith("crm_npd.metrics") for nome in _modulos_completos_importados(arvore)
    ), f"{arquivo.relative_to(PROJECT_ROOT)} inverte a dependencia domain <- metrics"


@pytest.mark.parametrize("arquivo", _modulos_puros(), ids=lambda p: p.name)
def test_funcoes_publicas_documentadas(arquivo: Path) -> None:
    """Toda funcao e classe publica das camadas puras precisa de docstring.

    As formulas sao o conhecimento de negocio da empresa. Uma formula sem
    explicacao e exatamente o problema que esta refatoracao existe para resolver.
    """
    arvore = ast.parse(arquivo.read_text(encoding="utf-8"), filename=str(arquivo))
    assert ast.get_docstring(arvore), f"{arquivo.name} sem docstring de modulo"

    sem_doc: list[str] = []
    for no in arvore.body:
        if isinstance(no, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if no.name.startswith("_"):
                continue
            if not ast.get_docstring(no):
                sem_doc.append(f"{no.name} (linha {no.lineno})")
    assert not sem_doc, (
        f"{arquivo.relative_to(PROJECT_ROOT)} tem definicao publica sem docstring: {sem_doc}"
    )
