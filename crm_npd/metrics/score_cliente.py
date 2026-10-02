"""Score de recompra de identidades comerciais.

O score responde a pergunta "quanto vale a pena ligar para este cliente agora".
Combina tres dimensoes independentes e aplica tetos que impedem cliente de compra
esparsa de aparecer como prioridade alta.

Dimensoes e pesos:

frequencia (55%)
    A media de recompra do cliente e boa para o negocio? Olha o intervalo
    mediano em termos absolutos, nao a situacao atual.

recompra (25%)
    O cliente esta dentro da janela esperada dele? Olha o atraso atual contra o
    proprio padrao. Um cliente de compra trimestral pontual pontua bem aqui
    mesmo com frequencia baixa.

valor (20%)
    Qual o tamanho do cliente em relacao ao maior da carteira analisada.
"""

from __future__ import annotations

from crm_npd.domain.enums import FaixaScore, PrioridadeTarefa
from crm_npd.domain.valores import EstatisticasIdentidade, ScoreIdentidade

#: Pesos das tres dimensoes no score final. Somam 1,0.
PESO_FREQUENCIA = 0.55
PESO_RECOMPRA = 0.25
PESO_VALOR = 0.20

#: Teto de score para quem tem no maximo uma compra registrada.
TETO_COMPRA_UNICA = 25.0

#: Tetos por intervalo mediano de recompra, em (dias_acima, teto).
#: Avaliados do intervalo maior para o menor; o primeiro que casar vale.
TETOS_POR_INTERVALO: tuple[tuple[float, float], ...] = (
    (240.0, 39.0),
    (180.0, 54.0),
    (120.0, 69.0),
)

#: Piso de score de cada faixa, do maior para o menor.
PISOS_FAIXA: tuple[tuple[float, FaixaScore], ...] = (
    (85.0, FaixaScore.ELITE),
    (70.0, FaixaScore.MUITO_FORTE),
    (55.0, FaixaScore.BOM),
    (35.0, FaixaScore.MORNO),
)


def limitar_score(valor: object, minimo: float = 0.0, maximo: float = 100.0) -> float:
    """Converte para float e limita ao intervalo valido de score.

    Aceita entrada invalida como zero, replicando a tolerancia do monolito, que
    recebia valores vindos do banco sem garantia de tipo.

    Args:
        valor: Valor a converter.
        minimo: Limite inferior.
        maximo: Limite superior.

    Returns:
        Float dentro de ``[minimo, maximo]``.
    """
    try:
        numero = float(valor)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        numero = 0.0
    return max(minimo, min(maximo, numero))


def faixa_de_score(score: float) -> FaixaScore:
    """Classifica uma nota na faixa comercial correspondente.

    Args:
        score: Nota de 0 a 100.

    Returns:
        Faixa da nota.
    """
    limitado = limitar_score(score)
    for piso, faixa in PISOS_FAIXA:
        if limitado >= piso:
            return faixa
    return FaixaScore.FRIO


def score_frequencia(intervalo_mediano: float | None, pedidos: int) -> float:
    """Pontua a qualidade da frequencia de recompra do cliente.

    Mede se a cadencia do cliente e boa para o negocio, em termos absolutos.
    Nao avalia se ele esta atrasado: isso e papel de :func:`score_recompra`.

    Args:
        intervalo_mediano: Mediana de dias entre compras, ou None se nao houver
            historico suficiente.
        pedidos: Numero de compras no historico.

    Returns:
        Pontuacao de 0 a 100.
    """
    if int(pedidos or 0) <= 1:
        return 15.0
    if intervalo_mediano is None:
        return 20.0

    intervalo = float(intervalo_mediano or 0)
    for limite, pontos in (
        (30.0, 100.0),
        (45.0, 95.0),
        (60.0, 88.0),
        (90.0, 75.0),
        (120.0, 60.0),
        (150.0, 45.0),
        (180.0, 32.0),
        (240.0, 20.0),
    ):
        if intervalo <= limite:
            return pontos
    return 8.0


def score_recompra(stats: EstatisticasIdentidade) -> float:
    """Pontua o comportamento atual do cliente contra o proprio padrao.

    Trabalha com a razao entre o atraso atual e o intervalo mediano. Razao 1,0
    significa que o cliente esta exatamente no ponto esperado de recompra.

    Args:
        stats: Historico consolidado da identidade.

    Returns:
        Pontuacao de 0 a 100.
    """
    if stats.pedidos <= 1:
        return 20.0
    if not stats.intervalo_mediano or stats.intervalo_mediano <= 0:
        return 30.0
    if stats.atraso_atual is None:
        return 40.0

    razao = float(stats.atraso_atual) / float(stats.intervalo_mediano)
    for limite, pontos in (
        (0.80, 100.0),
        (1.00, 90.0),
        (1.15, 75.0),
        (1.35, 55.0),
        (1.70, 35.0),
        (2.20, 18.0),
    ):
        if razao <= limite:
            return pontos
    return 5.0


def score_valor(valor_cliente: float, valor_referencia_max: float) -> float:
    """Pontua o tamanho do cliente em relacao ao maior da carteira analisada.

    E uma medida relativa: o mesmo cliente pontua diferente conforme o escopo
    consultado, porque a referencia muda.

    Args:
        valor_cliente: Faturamento acumulado do cliente.
        valor_referencia_max: Maior faturamento do escopo.

    Returns:
        Pontuacao de 0 a 100, ou zero se nao houver referencia valida.
    """
    cliente = float(valor_cliente or 0)
    referencia = float(valor_referencia_max or 0)
    if referencia <= 0:
        return 0.0

    pct = cliente / referencia
    for limite, pontos in (
        (0.90, 100.0),
        (0.70, 90.0),
        (0.50, 78.0),
        (0.30, 62.0),
        (0.15, 45.0),
        (0.05, 28.0),
    ):
        if pct >= limite:
            return pontos
    return 12.0


def aplicar_tetos(score: float, stats: EstatisticasIdentidade) -> float:
    """Aplica os tetos de negocio ao score bruto.

    Impede que a dimensao valor, sozinha, promova um cliente de compra rara a
    faixa alta. Um cliente de compra unica, por mais que fature, nao e carteira
    recorrente.

    Args:
        score: Score ja ponderado pelas tres dimensoes.
        stats: Historico consolidado da identidade.

    Returns:
        Score possivelmente reduzido pelo teto aplicavel.
    """
    if stats.pedidos <= 1:
        return min(score, TETO_COMPRA_UNICA)
    if stats.intervalo_mediano is None:
        return score

    intervalo = float(stats.intervalo_mediano or 0)
    for dias_acima, teto in TETOS_POR_INTERVALO:
        if intervalo > dias_acima:
            return min(score, teto)
    return score


def calcular_score(stats: EstatisticasIdentidade, valor_referencia_max: float) -> ScoreIdentidade:
    """Calcula o score completo de uma identidade comercial.

    Args:
        stats: Historico consolidado da identidade.
        valor_referencia_max: Maior faturamento do escopo, referencia da dimensao
            valor.

    Returns:
        Score final com faixa e as tres parciais.
    """
    frequencia = score_frequencia(stats.intervalo_mediano, stats.pedidos)
    recompra = score_recompra(stats)
    valor = score_valor(stats.valor_total, valor_referencia_max)

    # O arredondamento intermediario e deliberado: o monolito arredonda antes de
    # aplicar os tetos, e remover esse passo desloca notas na fronteira de faixa.
    bruto = round(
        frequencia * PESO_FREQUENCIA + recompra * PESO_RECOMPRA + valor * PESO_VALOR,
        1,
    )
    final = round(limitar_score(aplicar_tetos(bruto, stats)), 1)

    return ScoreIdentidade(
        score=final,
        faixa=faixa_de_score(final),
        frequencia=round(frequencia, 1),
        recompra=round(recompra, 1),
        valor=round(valor, 1),
    )


def esta_fora_da_janela(atraso_atual: int | None, intervalo_mediano: float | None) -> bool:
    """Indica se o cliente passou do proprio ponto esperado de recompra.

    Args:
        atraso_atual: Dias desde a ultima compra.
        intervalo_mediano: Mediana de dias entre compras.

    Returns:
        True quando o atraso alcancou ou passou o intervalo mediano.
    """
    if atraso_atual is None or not intervalo_mediano:
        return False
    try:
        return float(atraso_atual) >= float(intervalo_mediano)
    except (TypeError, ValueError):
        return False


def prioridade_tarefa(
    faixa: FaixaScore,
    atraso_atual: int | None,
    intervalo_mediano: float | None,
) -> PrioridadeTarefa:
    """Define a prioridade da tarefa de recompra de um cliente.

    Somente cliente de faixa alta pode chegar a URGENTE, e apenas quando esta
    fora da janela. Cliente fraco atrasado nao e urgente: e cliente fraco.

    Args:
        faixa: Faixa de score da identidade.
        atraso_atual: Dias desde a ultima compra.
        intervalo_mediano: Mediana de dias entre compras.

    Returns:
        Prioridade da tarefa.
    """
    faixa_alta = faixa in (FaixaScore.ELITE, FaixaScore.MUITO_FORTE)
    if faixa_alta and esta_fora_da_janela(atraso_atual, intervalo_mediano):
        return PrioridadeTarefa.URGENTE
    if faixa_alta:
        return PrioridadeTarefa.ALTA
    if faixa is FaixaScore.BOM:
        return PrioridadeTarefa.MEDIA
    return PrioridadeTarefa.BAIXA


#: Motivo exibido ao usuario para cada prioridade.
MOTIVO_PRIORIDADE: dict[PrioridadeTarefa, str] = {
    PrioridadeTarefa.URGENTE: "Cliente forte fora da janela ideal de recompra",
    PrioridadeTarefa.ALTA: "Cliente estratégico / muito bom",
    PrioridadeTarefa.MEDIA: "Cliente bom com potencial",
    PrioridadeTarefa.BAIXA: "Cliente de baixa prioridade comercial",
}
