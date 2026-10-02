"""Formulas de recompra e classificacao de situacao do cliente.

Atencao a um ponto historico: o CRM media o intervalo de recompra de duas formas
diferentes. O score e as listas de atraso usam a MEDIANA dos intervalos; a tela
de tarefas usava a MEDIA aritmetica. As duas estao aqui, explicitas e nomeadas,
para que a escolha deixe de ser acidental. Ver docs/02-dicionario-formulas.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from statistics import median

from crm_npd.domain.enums import ClassificacaoCliente

#: Multiplicador do intervalo mediano a partir do qual o cliente entra em risco.
#: Abaixo dele o cliente e ATIVO; acima, INATIVO.
FATOR_EM_RISCO = 1.3

#: Minimo de compras no historico para que se possa falar de padrao de recompra.
MINIMO_COMPRAS_PARA_PADRAO = 2


def datas_unicas_ordenadas(datas: Iterable[date]) -> list[date]:
    """Remove datas repetidas e ordena.

    Varias notas emitidas no mesmo dia sao uma unica ocasiao de compra. Contar
    cada nota separadamente infla a frequencia e distorce o intervalo.

    Args:
        datas: Datas de compra, em qualquer ordem e com repeticoes.

    Returns:
        Lista ordenada de datas distintas.
    """
    return sorted(set(datas))


def intervalos_entre_compras(datas: Sequence[date]) -> list[int]:
    """Calcula os intervalos em dias entre compras consecutivas.

    Args:
        datas: Datas de compra em ordem crescente.

    Returns:
        Lista de intervalos em dias, descartando intervalos negativos.
    """
    return [
        (datas[i] - datas[i - 1]).days
        for i in range(1, len(datas))
        if (datas[i] - datas[i - 1]).days >= 0
    ]


def intervalo_mediano(datas: Sequence[date]) -> int | None:
    """Mediana dos intervalos entre compras.

    Medida usada pelo score e pelas listas de atraso. A mediana resiste a um
    pedido fora do padrao, que distorceria a media.

    Args:
        datas: Datas de compra em ordem crescente.

    Returns:
        Mediana em dias inteiros, ou None se nao houver intervalo calculavel.
    """
    intervalos = intervalos_entre_compras(datas)
    if not intervalos:
        return None
    return int(median(intervalos))


def intervalo_medio(datas: Sequence[date]) -> float | None:
    """Media aritmetica dos intervalos entre compras.

    Medida usada historicamente pela tela de tarefas. Mantida explicita para que
    a divergencia com :func:`intervalo_mediano` seja visivel e auditavel.

    Args:
        datas: Datas de compra em ordem crescente.

    Returns:
        Media em dias, ou None se nao houver intervalo calculavel.
    """
    intervalos = intervalos_entre_compras(datas)
    if not intervalos:
        return None
    return sum(intervalos) / len(intervalos)


def atraso_atual(ultima_compra: date | None, referencia: date) -> int | None:
    """Dias desde a ultima compra ate a data de referencia.

    Args:
        ultima_compra: Data da ultima compra, ou None.
        referencia: Data de referencia da analise.

    Returns:
        Dias de atraso, ou None se nao houver ultima compra.
    """
    if ultima_compra is None:
        return None
    return (referencia - ultima_compra).days


def classificar_situacao(
    atraso: int | None,
    intervalo: float | None,
) -> ClassificacaoCliente:
    """Classifica a situacao de recompra do cliente.

    Sem padrao de recompra conhecido o cliente e NOVO, nao INATIVO: nao se pode
    acusar atraso de quem ainda nao estabeleceu cadencia.

    Args:
        atraso: Dias desde a ultima compra.
        intervalo: Intervalo mediano de recompra em dias.

    Returns:
        Classificacao da situacao.
    """
    if not intervalo:
        return ClassificacaoCliente.NOVO
    if atraso is None:
        return ClassificacaoCliente.NOVO
    if atraso <= intervalo:
        return ClassificacaoCliente.ATIVO
    if atraso <= intervalo * FATOR_EM_RISCO:
        return ClassificacaoCliente.EM_RISCO
    return ClassificacaoCliente.INATIVO


def e_cliente_recuperado(
    data_movimento: date,
    historico_anterior: Sequence[date],
) -> bool:
    """Decide se uma compra representa recuperacao de cliente.

    Recuperado e quem voltou a comprar depois de ficar parado mais tempo que o
    proprio intervalo tipico. E um subconjunto da recorrencia, nao uma terceira
    categoria paralela: quem e recuperado tambem conta como recompra.

    Exige pelo menos duas compras anteriores, porque com uma unica compra nao ha
    intervalo tipico contra o qual comparar o periodo de ausencia.

    Args:
        data_movimento: Data da compra sendo avaliada.
        historico_anterior: Datas de compra estritamente anteriores a
            ``data_movimento``, em ordem crescente.

    Returns:
        True quando a ausencia superou o intervalo mediano do cliente.
    """
    if len(historico_anterior) < MINIMO_COMPRAS_PARA_PADRAO:
        return False
    intervalos = intervalos_entre_compras(historico_anterior)
    if not intervalos:
        return False
    mediana = float(median(intervalos))
    intervalo_ausencia = (data_movimento - historico_anterior[-1]).days
    return mediana > 0 and intervalo_ausencia > mediana


def esta_atrasado(atraso: int | None, intervalo: float | None) -> bool:
    """Indica se o cliente passou do intervalo tipico de recompra.

    Args:
        atraso: Dias desde a ultima compra.
        intervalo: Intervalo mediano de recompra.

    Returns:
        True quando o atraso supera o intervalo.
    """
    if atraso is None or not intervalo:
        return False
    return float(atraso) > float(intervalo)


def dias_fora_da_media(dias_desde_ultima: int | None, media: float | None) -> int:
    """Quantos dias o cliente passou da propria media de recompra.

    Formula da tela de tarefas. Diferente do score, que trabalha com a razao
    entre atraso e intervalo em vez da diferenca absoluta.

    Args:
        dias_desde_ultima: Dias desde a ultima compra.
        media: Media de dias entre compras.

    Returns:
        Dias excedentes, nunca negativo.
    """
    if dias_desde_ultima is None or media is None:
        return 0
    return max(0, round(float(dias_desde_ultima) - float(media)))


def ticket_medio(valores: Sequence[float]) -> float:
    """Media simples de uma serie de valores de compra.

    Args:
        valores: Valores das compras.

    Returns:
        Ticket medio, ou zero se a serie estiver vazia.
    """
    if not valores:
        return 0.0
    return sum(valores) / len(valores)
