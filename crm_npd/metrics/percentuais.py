"""Percentuais, variacoes e projecoes.

Funcoes pequenas, mas responsaveis por boa parte dos numeros da tela. O cuidado
central aqui e divisao por zero: o monolito tinha tres tratamentos diferentes para
base zero, e as diferencas estao documentadas em cada funcao.
"""

from __future__ import annotations

from crm_npd.domain.valores import PainelMeta

#: Tolerancia para considerar uma base como zero em comparacao de float.
EPSILON = 1e-9


def percentual(parte: float, total: float, padrao: float = 0.0) -> float:
    """Percentual de uma parte sobre um total.

    Args:
        parte: Numerador.
        total: Denominador.
        padrao: Valor devolvido quando o total e zero.

    Returns:
        Percentual de 0 a 100 (pode passar de 100 se a parte exceder o total).
    """
    try:
        total_f = float(total or 0)
        if total_f == 0:
            return float(padrao)
        return float(parte or 0) / total_f * 100.0
    except (TypeError, ValueError):
        return float(padrao)


def variacao_percentual(atual: float, anterior: float, padrao_base_zero: float = 0.0) -> float:
    """Variacao percentual entre dois periodos.

    Quando a base anterior e zero e o atual nao e, devolve 100% por convencao.
    Matematicamente a variacao seria infinita; 100% e a escolha que o CRM sempre
    exibiu e trocar isso mudaria o historico de comparativos.

    Args:
        atual: Valor do periodo atual.
        anterior: Valor do periodo anterior.
        padrao_base_zero: Valor devolvido quando ambos sao zero.

    Returns:
        Variacao em pontos percentuais.
    """
    try:
        atual_f = float(atual or 0)
        anterior_f = float(anterior or 0)
        if anterior_f == 0:
            return float(padrao_base_zero) if atual_f == 0 else 100.0
        return (atual_f - anterior_f) / anterior_f * 100.0
    except (TypeError, ValueError):
        return float(padrao_base_zero)


def variacao_percentual_ou_nulo(atual: float, anterior: float) -> float | None:
    """Variacao percentual que devolve None quando nao ha base de comparacao.

    Variante usada pela tela de performance. A diferenca em relacao a
    :func:`variacao_percentual` e deliberada: ali, "sem base anterior" precisa ser
    distinguivel de "cresceu 100%", porque alimenta a dimensao evolucao do score,
    onde None significa "nao pontua" em vez de "pontua o maximo".

    Usa valor absoluto no denominador, preservando o sinal da variacao quando a
    base anterior e negativa.

    Args:
        atual: Valor do periodo atual.
        anterior: Valor do periodo anterior.

    Returns:
        Variacao em pontos percentuais, ou None se nao havia base e houve
        movimento.
    """
    atual_f = float(atual or 0)
    anterior_f = float(anterior or 0)
    if abs(anterior_f) < EPSILON:
        return None if abs(atual_f) > EPSILON else 0.0
    return (atual_f - anterior_f) / abs(anterior_f) * 100.0


def projecao_linear(total_atual: float, dias_decorridos: int, dias_totais: int) -> float:
    """Projeta o fechamento do periodo pela media diaria realizada.

    Assume ritmo constante. Nao modela sazonalidade nem concentracao de
    faturamento no fim do mes, o que em geral faz a projecao do inicio do periodo
    subestimar o resultado.

    Args:
        total_atual: Valor realizado ate agora.
        dias_decorridos: Dias ja transcorridos no periodo.
        dias_totais: Total de dias do periodo.

    Returns:
        Valor projetado para o fim do periodo, ou zero se nada transcorreu.
    """
    if dias_decorridos <= 0:
        return 0.0
    return float(total_atual or 0.0) / dias_decorridos * dias_totais


def painel_meta(faturado: float, meta: float, dias_uteis_restantes: int) -> PainelMeta:
    """Monta o acompanhamento de meta exibido nos cards.

    Args:
        faturado: Valor realizado no periodo.
        meta: Meta do periodo.
        dias_uteis_restantes: Dias uteis que ainda restam.

    Returns:
        Painel com percentual atingido, quanto falta e necessidade diaria.
    """
    meta_f = float(meta or 0.0)
    faturado_f = float(faturado or 0.0)
    falta = max(meta_f - faturado_f, 0.0)
    return PainelMeta(
        pct=(faturado_f / meta_f * 100.0) if meta_f else 0.0,
        falta=falta,
        por_dia=(falta / dias_uteis_restantes) if dias_uteis_restantes > 0 else 0.0,
    )


def ticket_medio(valor_total: float, quantidade: int) -> float:
    """Valor medio por documento.

    Args:
        valor_total: Soma dos valores.
        quantidade: Numero de documentos.

    Returns:
        Ticket medio, ou zero se a quantidade for zero.
    """
    if not quantidade:
        return 0.0
    return float(valor_total or 0.0) / quantidade


def media_simples(valores: list[float]) -> float:
    """Media aritmetica de uma lista, tolerante a lista vazia.

    Args:
        valores: Valores a promediar.

    Returns:
        Media, ou zero se a lista estiver vazia.
    """
    if not valores:
        return 0.0
    return sum(valores) / len(valores)
