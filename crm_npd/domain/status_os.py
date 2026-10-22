"""Cadastro de STATUS_OS do Sisplan (tipo PED).

A tela de orcamento agrupa pelo texto gravado em ``desc_status``. Quando a
sincronizacao so trouxe o codigo (``40``, ``09``), este mapa devolve a
descricao do cadastro para exibir ``40 - MANUTENCAO DE CARTEIRA``.
"""

from __future__ import annotations

# Codigo do cadastro Sisplan (TIPO=PED) -> descricao. Fonte: tela de cadastro.
STATUS_OS_PED: dict[str, str] = {
    "08": "AGUARDANDO CADASTRO DE PRODUTO",
    "09": "ORCAMENTO EM NEGOCIACAO",
    "10": "AGUARDANDO CADASTRO DE ARTE",
    "11": "LIBERADO PARA SEPARACAO",
    "12": "AGUARDANDO APROVACAO DE ARTE",
    "13": "GERAR PEDIDO",
    "14": "AGUARDANDO LIBERACAO FINANCEIRO",
    "15": "LIBERADO PARA PCP",
    "16": "RESTRICAO FINANCEIRA",
    "17": "EM PRODUCAO",
    "18": "PEDIDO FATURADO",
    "19": "PEDIDO APROVADO",
    "21": "CANCELADO PELO CLIENTE",
    "28": "REALIZAR CONTATO FUTURO",
    "29": "CLIENTE NAO RETORNOU",
    "31": "CLIENTE SEM PERFIL DE COMPRA",
    "32": "CLIENTE BUSCA OUTRO PRODUTO",
    "33": "ORCAMENTO PERDIDO PRECO",
    "34": "ORCAMENTO PERDIDO QUANTIDADE MINI",
    "37": "ORCAMENTO ENCAMINHADO PARA VENDEDOR",
    "38": "PESQUISA DE PRECO",
    "39": "AGUARDANDO COTACAO DA LOGISTICA",
    "40": "MANUTENCAO DE CARTEIRA",
    "41": "EM DEFINICAO FINANCEIRA",
    "42": "ORCAMENTO INICIAL",
    "43": "MANUTENCAO DE INATIVO",
    "44": "AGUARDANDO PAGAMENTO",
}


def codigo_status_os(valor: str | None) -> str:
    """Extrai o codigo numerico de ``40`` ou ``40 - MANUTENCAO DE CARTEIRA``."""
    texto = str(valor or "").strip()
    if not texto:
        return ""
    cabeca = texto.split(" - ", 1)[0].strip()
    if cabeca.isdigit():
        return cabeca.zfill(2)
    if texto.isdigit():
        return texto.zfill(2)
    return ""


def descricao_status_os(codigo: str | None) -> str:
    """Descricao do cadastro Sisplan para o codigo, ou vazio."""
    return STATUS_OS_PED.get(codigo_status_os(codigo), "")


def rotulo_status_os(codigo: str | None, descricao: str | None = None) -> str:
    """Monta ``40 - MANUTENCAO DE CARTEIRA``.

    Se ``descricao`` vier vazia ou for so o codigo, usa o cadastro local.
    """
    bruto = str(codigo or "").strip()
    desc = str(descricao or "").strip()
    ja_rotulado = desc or bruto
    if " - " in ja_rotulado and codigo_status_os(ja_rotulado):
        return ja_rotulado

    if desc and codigo_status_os(desc) and " - " not in desc:
        desc = ""

    cod = codigo_status_os(bruto) or codigo_status_os(desc)
    if not desc:
        desc = STATUS_OS_PED.get(cod, "")
    if cod and desc:
        return f"{cod} - {desc}"
    return desc or bruto
