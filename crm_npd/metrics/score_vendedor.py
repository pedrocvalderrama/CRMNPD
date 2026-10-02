"""Score mensal de performance comercial do vendedor, de 0 a 100.

Diferente do score de cliente, este score e RELATIVO a equipe: cada dimensao e
normalizada contra o melhor desempenho da propria equipe no mes. Um vendedor pode
cair de score sem piorar, se a equipe melhorou.

A comparacao acontece dentro do canal (Interno ou Externo) e nunca entre canais,
porque a base de calculo e o ciclo de venda dos dois sao diferentes.

Pesos: atividade 50%, pipeline 15%, conversao 10%, resultado 10%, carteira 10%,
evolucao 5%. Atividade pesa mais porque e a unica dimensao sob controle direto e
diario do vendedor.
"""

from __future__ import annotations

import math

from crm_npd.domain.valores import DimensoesScoreVendedor

PESO_ATIVIDADE = 0.50
PESO_PIPELINE = 0.15
PESO_CONVERSAO = 0.10
PESO_RESULTADO = 0.10
PESO_CARTEIRA = 0.10
PESO_EVOLUCAO = 0.05

#: Pontos de carteira por tipo de cliente conquistado no periodo.
#: Recuperado vale mais que recompra normal, menos que cliente novo.
PONTOS_CLIENTE_NOVO = 1.0
PONTOS_CLIENTE_RECUPERADO = 0.8
PONTOS_CLIENTE_RECORRENTE = 0.30

#: Taxa de conversao, em pontos percentuais, tratada como desempenho maximo.
CONVERSAO_DE_REFERENCIA = 60.0

#: Numero de orcamentos a partir do qual a conversao e considerada confiavel.
#: Abaixo disso a taxa e amortecida, para que 1 ganho em 1 orcamento nao valha
#: 100% de conversao.
AMOSTRA_CONFIAVEL_ORCAMENTOS = 30.0

#: Limiares de variacao para a dimensao evolucao.
QUEDA_TOLERADA_ORCAMENTOS_PCT = -10.0
QUEDA_TOLERADA_CONVERSAO_PP = -2.0

#: Pisos de score para o rotulo exibido, do maior para o menor.
ROTULOS: tuple[tuple[float, str, str], ...] = (
    (85.0, "Destaque", "success"),
    (70.0, "Forte", "primary"),
    (50.0, "Regular", "info"),
    (30.0, "Atenção", "warning"),
)
ROTULO_MINIMO = ("Baixa atividade", "danger")


def _proporcao_simples(valor: float, maximo: float) -> float:
    """Percentual linear de um valor contra o maximo da equipe."""
    if maximo <= 0:
        return 0.0
    return min(100.0, max(0.0, float(valor or 0.0)) / maximo * 100.0)


def _proporcao_amortecida(valor: float, maximo: float) -> float:
    """Percentual com raiz quadrada, que comprime a vantagem do primeiro colocado.

    Usado em valor de pipeline e em faturamento. Sem o amortecimento, um unico
    vendedor com um pedido muito grande zeraria a dimensao de todos os outros.
    """
    if maximo <= 0:
        return 0.0
    razao = max(0.0, float(valor or 0.0) / maximo)
    return min(100.0, math.sqrt(razao) * 100.0)


def pontos_carteira(novos: int, repeticoes: int, recuperados: int) -> float:
    """Pontua a qualidade da carteira movimentada no periodo.

    Recuperados ja estao contidos em ``repeticoes``, entao sao descontados dali
    antes de receberem o peso maior. Sem isso, um cliente recuperado seria contado
    duas vezes.

    Args:
        novos: Identidades que compraram pela primeira vez no periodo.
        repeticoes: Identidades recorrentes, incluindo as recuperadas.
        recuperados: Identidades que voltaram apos ausencia acima do padrao.

    Returns:
        Pontuacao bruta de carteira, ainda nao normalizada pela equipe.
    """
    qtd_novos = max(0.0, float(novos or 0))
    qtd_reps = max(0.0, float(repeticoes or 0))
    qtd_recup = min(qtd_reps, max(0.0, float(recuperados or 0)))
    recorrentes_normais = max(0.0, qtd_reps - qtd_recup)
    return (
        qtd_novos * PONTOS_CLIENTE_NOVO
        + qtd_recup * PONTOS_CLIENTE_RECUPERADO
        + recorrentes_normais * PONTOS_CLIENTE_RECORRENTE
    )


def dimensao_conversao(conversao_pct: float, orcamentos: int) -> float:
    """Normaliza a taxa de conversao, amortecida por tamanho de amostra.

    Args:
        conversao_pct: Taxa de conversao do vendedor em pontos percentuais.
        orcamentos: Numero de orcamentos abertos no periodo.

    Returns:
        Percentual de 0 a 100.
    """
    fator_amostra = min(
        1.0, math.sqrt(max(0.0, float(orcamentos or 0)) / AMOSTRA_CONFIAVEL_ORCAMENTOS)
    )
    efetiva = float(conversao_pct or 0.0) * fator_amostra
    return min(100.0, efetiva / CONVERSAO_DE_REFERENCIA * 100.0)


def dimensao_evolucao(
    variacao_orcamentos_pct: float | None,
    variacao_conversao_pp: float,
) -> float:
    """Pontua o momento do vendedor contra o periodo anterior.

    Duas metades independentes de 50 pontos: volume de orcamentos e conversao.
    Cada metade concede pontuacao parcial para queda pequena, reconhecendo que
    oscilacao leve nao e piora real.

    Args:
        variacao_orcamentos_pct: Variacao percentual no numero de orcamentos, ou
            None quando nao havia base anterior.
        variacao_conversao_pp: Variacao da conversao em pontos percentuais.

    Returns:
        Pontuacao de 0 a 100.
    """
    pontos = 0.0
    if variacao_orcamentos_pct is not None:
        if variacao_orcamentos_pct > 0:
            pontos += 50.0
        elif variacao_orcamentos_pct >= QUEDA_TOLERADA_ORCAMENTOS_PCT:
            pontos += 25.0

    if variacao_conversao_pp > 0:
        pontos += 50.0
    elif variacao_conversao_pp >= QUEDA_TOLERADA_CONVERSAO_PP:
        pontos += 25.0
    return pontos


def calcular_dimensoes(
    *,
    orcamentos_qtd: int,
    orcamentos_valor: float,
    faturamento_valor: float,
    conversao_pct: float,
    carteira_pontos: float,
    variacao_orcamentos_pct: float | None,
    variacao_conversao_pp: float,
    max_orcamentos_qtd: float,
    max_orcamentos_valor: float,
    max_faturamento: float,
    max_carteira_pontos: float,
) -> DimensoesScoreVendedor:
    """Calcula as seis dimensoes do score, normalizadas pela equipe.

    Args:
        orcamentos_qtd: Orcamentos abertos pelo vendedor no periodo.
        orcamentos_valor: Valor dos orcamentos abertos.
        faturamento_valor: Faturamento do vendedor no periodo.
        conversao_pct: Taxa de conversao do vendedor.
        carteira_pontos: Pontos brutos de carteira, de :func:`pontos_carteira`.
        variacao_orcamentos_pct: Variacao de orcamentos contra o periodo anterior.
        variacao_conversao_pp: Variacao de conversao em pontos percentuais.
        max_orcamentos_qtd: Maior quantidade de orcamentos da equipe.
        max_orcamentos_valor: Maior valor de orcamentos da equipe.
        max_faturamento: Maior faturamento da equipe.
        max_carteira_pontos: Maior pontuacao de carteira da equipe.

    Returns:
        As seis dimensoes, cada uma de 0 a 100.
    """
    return DimensoesScoreVendedor(
        atividade=_proporcao_simples(orcamentos_qtd, max_orcamentos_qtd),
        pipeline=_proporcao_amortecida(orcamentos_valor, max_orcamentos_valor),
        conversao=dimensao_conversao(conversao_pct, orcamentos_qtd),
        resultado=_proporcao_amortecida(faturamento_valor, max_faturamento),
        carteira=_proporcao_simples(carteira_pontos, max_carteira_pontos),
        evolucao=dimensao_evolucao(variacao_orcamentos_pct, variacao_conversao_pp),
    )


def calcular_score(dimensoes: DimensoesScoreVendedor) -> float:
    """Combina as dimensoes nos pesos definidos.

    Args:
        dimensoes: Dimensoes normalizadas do vendedor.

    Returns:
        Score de 0 a 100, com uma casa decimal.
    """
    bruto = (
        dimensoes.atividade * PESO_ATIVIDADE
        + dimensoes.pipeline * PESO_PIPELINE
        + dimensoes.conversao * PESO_CONVERSAO
        + dimensoes.resultado * PESO_RESULTADO
        + dimensoes.carteira * PESO_CARTEIRA
        + dimensoes.evolucao * PESO_EVOLUCAO
    )
    return round(max(0.0, min(100.0, bruto)), 1)


def rotular_score(score: float) -> tuple[str, str]:
    """Traduz o score no rotulo e na cor exibidos.

    Args:
        score: Score de 0 a 100.

    Returns:
        Par (rotulo, cor Bootstrap).
    """
    for piso, rotulo, cor in ROTULOS:
        if score >= piso:
            return rotulo, cor
    return ROTULO_MINIMO


def score_anual(scores_mensais: list[float]) -> float:
    """Media aritmetica dos scores mensais acumulados no ano.

    Nao e ponderada por faturamento nem por numero de dias: cada mes fechado vale
    o mesmo. Um mes excepcional nao compensa varios meses fracos.

    Args:
        scores_mensais: Scores de janeiro ate o mes de referencia.

    Returns:
        Media dos scores, ou zero se a lista estiver vazia.
    """
    if not scores_mensais:
        return 0.0
    return sum(scores_mensais) / len(scores_mensais)
