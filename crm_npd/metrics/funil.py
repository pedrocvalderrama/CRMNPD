"""Formulas do funil comercial, derivado do relatorio 104 do Sisplan.

O funil do CRM e uma leitura somente de escrita do ERP: a etapa nunca e editada
no CRM, e sempre deduzida do status atual do orcamento. Se a negociacao avanca, o
status muda no Sisplan e a proxima sincronizacao reflete a mudanca.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence

from crm_npd.domain.enums import EtapaFunil
from crm_npd.domain.valores import MetricasPipeline

#: Dias em aberto a partir dos quais um orcamento acionavel conta como parado.
SLA_DIAS_PARADO = 14

#: Codigo de status do 104 que marca registro finalizado sem desfecho comercial.
CODIGO_STATUS_FINALIZADO = "F"

#: Textos de ``desc_status`` que indicam ganho.
TEXTOS_GANHO: tuple[str, ...] = ("PEDIDO APROVADO", "PEDIDO FATURADO")

#: Textos que indicam perda.
TEXTOS_PERDIDO: tuple[str, ...] = ("CANCELADO", "PERDIDO", "SEM PERFIL")

#: Textos que indicam espera por aprovacao interna ou financeira.
TEXTOS_APROVACAO: tuple[str, ...] = (
    "AGUARDANDO",
    "LIBERADO PARA",
    "GERAR PEDIDO",
    "RESTRICAO FINANCEIRA",
)

#: Textos que indicam negociacao em andamento.
TEXTOS_NEGOCIACAO: tuple[str, ...] = (
    "NEGOCIACAO",
    "PESQUISA DE PRECO",
    "BUSCA OUTRO PRODUTO",
)

#: Textos que indicam contato feito, sem negociacao efetiva ainda.
TEXTOS_CONTATO: tuple[str, ...] = (
    "CONTATO FUTURO",
    "NAO RETORNOU",
    "ENCAMINHADO PARA VENDEDOR",
    "MANUTENCAO DE CARTEIRA",
    "MANUTENCAO DE INATIVO",
)


def normalizar_texto(texto: str | None) -> str:
    """Normaliza texto do ERP para comparacao: sem acento, maiusculo, sem espaco extra.

    Args:
        texto: Texto cru vindo do relatorio.

    Returns:
        Texto normalizado, pronto para busca de subcadeia.
    """
    sem_acento = "".join(
        ch
        for ch in unicodedata.normalize("NFKD", texto or "")
        if not unicodedata.combining(ch)
    )
    return " ".join(sem_acento.upper().split())


def determinar_etapa(
    desc_status: str | None,
    pedido_id: str | None = None,
    status_code: str | None = None,
) -> EtapaFunil:
    """Deduz a etapa do funil a partir do estado do orcamento no ERP.

    A ordem de verdade e deliberada e nao pode ser reordenada:

    1. Vinculo com pedido ganha de tudo. Se o orcamento virou pedido, foi ganho,
       nao importa o que o texto de status diga.
    2. Texto de desfecho explicito (aprovado, faturado, cancelado, perdido).
    3. Codigo ``F``, que no 104 marca registro finalizado. Sem pedido e sem texto
       de desfecho, sai do pipeline como ENCERRADO, sem inventar motivo comercial.
    4. Textos de etapas ainda abertas.
    5. Default NOVO.

    Args:
        desc_status: Descricao do status no relatorio 104.
        pedido_id: Numero do pedido gerado, se o orcamento converteu.
        status_code: Codigo de status do 104.

    Returns:
        Etapa do funil.
    """
    pedido = str(pedido_id or "").strip()
    if pedido and pedido != "0":
        return EtapaFunil.GANHO

    descricao = normalizar_texto(desc_status)
    codigo = normalizar_texto(status_code)

    if any(texto in descricao for texto in TEXTOS_GANHO):
        return EtapaFunil.GANHO
    if any(texto in descricao for texto in TEXTOS_PERDIDO):
        return EtapaFunil.PERDIDO
    if codigo == CODIGO_STATUS_FINALIZADO:
        return EtapaFunil.ENCERRADO
    if any(texto in descricao for texto in TEXTOS_APROVACAO):
        return EtapaFunil.APROVACAO
    if any(texto in descricao for texto in TEXTOS_NEGOCIACAO):
        return EtapaFunil.NEGOCIACAO
    if any(texto in descricao for texto in TEXTOS_CONTATO):
        return EtapaFunil.CONTATO
    return EtapaFunil.NOVO


def valor_ponderado(valor: float, etapa: EtapaFunil) -> float:
    """Pondera o valor de um orcamento pela probabilidade da etapa.

    Args:
        valor: Valor total do orcamento.
        etapa: Etapa atual.

    Returns:
        Valor esperado do orcamento.
    """
    return float(valor or 0.0) * (etapa.probabilidade / 100.0)


def esta_parado(etapa: EtapaFunil, dias_aberto: int) -> bool:
    """Indica se um orcamento acionavel estourou o SLA.

    Apenas etapas acionaveis contam: um orcamento ganho ou perdido nao esta
    parado, esta concluido.

    Args:
        etapa: Etapa atual.
        dias_aberto: Dias desde a abertura do orcamento.

    Returns:
        True quando a etapa e acionavel e o SLA foi estourado.
    """
    return etapa.e_acionavel and int(dias_aberto or 0) >= SLA_DIAS_PARADO


def taxa_conversao(ganhos: int, fechados: int) -> float:
    """Percentual de ganho sobre os orcamentos com desfecho conhecido.

    O denominador considera somente GANHO e PERDIDO. ENCERRADO sai do pipeline,
    mas nao entra na conta: incluir registro finalizado sem desfecho comercial
    deprimiria artificialmente a taxa de ganho.

    Args:
        ganhos: Orcamentos ganhos.
        fechados: Orcamentos ganhos mais perdidos.

    Returns:
        Percentual de 0 a 100, ou zero se nao houver fechados.
    """
    if fechados <= 0:
        return 0.0
    return ganhos / fechados * 100.0


def calcular_metricas(
    itens: Iterable[tuple[EtapaFunil, float, int]],
) -> MetricasPipeline:
    """Consolida as metricas de pipeline de um conjunto de orcamentos.

    Args:
        itens: Triplas de (etapa, valor_total, dias_aberto).

    Returns:
        Metricas agregadas do funil.
    """
    materializados: Sequence[tuple[EtapaFunil, float, int]] = tuple(itens)

    ativos = [item for item in materializados if not item[0].e_final]
    fechados = [
        item
        for item in materializados
        if item[0] in (EtapaFunil.GANHO, EtapaFunil.PERDIDO)
    ]
    ganhos = [item for item in fechados if item[0] is EtapaFunil.GANHO]
    acionaveis = [item for item in ativos if item[0].e_acionavel]

    return MetricasPipeline(
        ativos=len(ativos),
        pipeline=sum(float(valor or 0.0) for _, valor, _ in ativos),
        ponderado=sum(valor_ponderado(valor, etapa) for etapa, valor, _ in ativos),
        ganhos=len(ganhos),
        fechados=len(fechados),
        conversao=taxa_conversao(len(ganhos), len(fechados)),
        acionaveis=len(acionaveis),
        parados=sum(1 for etapa, _, dias in acionaveis if esta_parado(etapa, dias)),
    )
