"""Calculo de periodos e ciclos.

O CRM trabalha com DOIS calendarios diferentes, e confundi-los e a causa mais
comum de numero que "nao bate" entre telas:

ciclo de faturamento (financeiro)
    Vai do dia seguinte ao fechamento do mes anterior ate o dia de fechamento do
    mes de referencia. Com fechamento no dia 24, abril vai de 25/03 a 24/04. O dia
    de fechamento e configuravel por mes, em ``ciclos_faturamento``. Usado para
    faturamento, comissao e mix Novo/Recorrente.

mes calendario (comercial)
    Dia 1 ao ultimo dia do mes. Usado para pedidos, metas e projecao.

Estas funcoes sao puras: o dia de fechamento entra como argumento. No monolito,
``periodo_ciclo_faturamento_manual`` abria conexao com o banco dentro do calculo
de datas, o que tornava impossivel testar a regra sem um banco montado.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from crm_npd.domain.valores import Periodo

#: Dia de fechamento do ciclo de faturamento quando nao ha configuracao do mes.
DIA_FECHAMENTO_PADRAO = 24


def dia_valido_no_mes(ano: int, mes: int, dia: int) -> date:
    """Monta uma data limitando o dia ao ultimo dia existente no mes.

    Necessario porque o fechamento configurado pode ser 31 e o mes ter 28 dias.

    Args:
        ano: Ano.
        mes: Mes de 1 a 12.
        dia: Dia desejado; sera limitado ao intervalo valido do mes.

    Returns:
        Data valida.
    """
    ultimo = calendar.monthrange(ano, mes)[1]
    return date(ano, mes, min(max(1, int(dia)), ultimo))


def normalizar_dia_fechamento(dia: int | None) -> int:
    """Limita o dia de fechamento ao intervalo aceito.

    Args:
        dia: Dia configurado, possivelmente invalido ou ausente.

    Returns:
        Dia entre 1 e 31; o padrao quando a entrada e invalida.
    """
    try:
        valor = int(dia) if dia is not None else DIA_FECHAMENTO_PADRAO
    except (TypeError, ValueError):
        valor = DIA_FECHAMENTO_PADRAO
    return max(1, min(31, valor))


def ciclo_faturamento(
    referencia: date,
    dia_fechamento: int = DIA_FECHAMENTO_PADRAO,
) -> Periodo:
    """Ciclo de faturamento do mes de referencia.

    Args:
        referencia: Qualquer data dentro do mes desejado.
        dia_fechamento: Dia de fechamento configurado para o mes.

    Returns:
        Periodo do ciclo, comecando no dia seguinte ao fechamento anterior.

    Examples:
        Com fechamento no dia 24, abril de 2026 vai de 25/03 a 24/04:

        >>> ciclo_faturamento(date(2026, 4, 10), 24)
        Periodo(inicio=datetime.date(2026, 3, 25), fim=datetime.date(2026, 4, 24))
    """
    dia = normalizar_dia_fechamento(dia_fechamento)
    fim = dia_valido_no_mes(referencia.year, referencia.month, dia)

    if referencia.month == 1:
        ano_anterior, mes_anterior = referencia.year - 1, 12
    else:
        ano_anterior, mes_anterior = referencia.year, referencia.month - 1

    inicio = dia_valido_no_mes(ano_anterior, mes_anterior, dia) + timedelta(days=1)
    return Periodo(inicio=inicio, fim=fim)


def ciclo_faturamento_ate_hoje(
    referencia: date,
    hoje: date,
    dia_fechamento: int = DIA_FECHAMENTO_PADRAO,
) -> Periodo:
    """Ciclo de faturamento truncado na data atual.

    Para o mes em andamento, limita o fim em ``hoje``, de modo que projecoes e
    comparacoes nao contem dias que ainda nao aconteceram. Para mes ja fechado,
    devolve o ciclo completo.

    Args:
        referencia: Qualquer data dentro do mes desejado.
        hoje: Data atual.
        dia_fechamento: Dia de fechamento configurado para o mes.

    Returns:
        Periodo do ciclo, com o fim limitado a ``hoje``.
    """
    ciclo = ciclo_faturamento(referencia, dia_fechamento)
    return Periodo(inicio=ciclo.inicio, fim=min(hoje, ciclo.fim))


def ultimo_dia_do_mes(referencia: date) -> date:
    """Ultimo dia do mes da data informada.

    Args:
        referencia: Qualquer data do mes.

    Returns:
        Ultimo dia do mes.
    """
    return date(
        referencia.year, referencia.month, calendar.monthrange(referencia.year, referencia.month)[1]
    )


def mes_calendario(referencia: date) -> Periodo:
    """Mes civil completo, do dia 1 ao ultimo dia.

    Args:
        referencia: Qualquer data dentro do mes desejado.

    Returns:
        Periodo do mes calendario.
    """
    return Periodo(
        inicio=date(referencia.year, referencia.month, 1),
        fim=ultimo_dia_do_mes(referencia),
    )


def dias_uteis(periodo: Periodo) -> int:
    """Conta dias de segunda a sexta dentro do periodo.

    Nao desconta feriado: o monolito nunca manteve calendario de feriados, e
    introduzir um aqui mudaria toda meta diaria e projecao do historico.

    Args:
        periodo: Periodo a contar.

    Returns:
        Quantidade de dias uteis, contando as duas pontas.
    """
    total = 0
    atual = periodo.inicio
    while atual <= periodo.fim:
        if atual.weekday() < 5:
            total += 1
        atual += timedelta(days=1)
    return total


def dias_uteis_restantes(periodo: Periodo, hoje: date) -> int:
    """Dias uteis de hoje ate o fim do periodo, inclusive.

    Args:
        periodo: Periodo de referencia.
        hoje: Data atual.

    Returns:
        Dias uteis restantes; zero se o periodo ja terminou.
    """
    if hoje > periodo.fim:
        return 0
    inicio = max(hoje, periodo.inicio)
    return dias_uteis(Periodo(inicio=inicio, fim=periodo.fim))


def mes_anterior(referencia: date) -> date:
    """Primeiro dia do mes anterior ao da referencia.

    Args:
        referencia: Qualquer data do mes.

    Returns:
        Dia 1 do mes anterior.
    """
    if referencia.month == 1:
        return date(referencia.year - 1, 12, 1)
    return date(referencia.year, referencia.month - 1, 1)


def ultimos_meses(referencia: date, quantidade: int) -> list[date]:
    """Lista os primeiros dias dos ultimos meses, do mais antigo ao mais recente.

    Base da meta automatica, que usa a media dos ultimos ciclos fechados.

    Args:
        referencia: Mes de referencia, que NAO entra na lista.
        quantidade: Quantos meses anteriores listar.

    Returns:
        Lista de datas no dia 1, em ordem crescente.
    """
    meses: list[date] = []
    atual = date(referencia.year, referencia.month, 1)
    for _ in range(max(0, quantidade)):
        atual = mes_anterior(atual)
        meses.append(atual)
    return list(reversed(meses))
