"""Formulas de comissao.

E o modulo de maior risco financeiro do sistema: qualquer alteracao aqui muda
dinheiro pago a vendedor. Toda regra esta documentada e coberta por teste.

Regras vigentes, extraidas de ``compute_vendor_commission`` e
``linhas_comissao_externo_liquidez`` do monolito:

Vendedor INTERNO
    Base e o faturamento (relatorio 731). Cada linha de nota rende 1%. Linha de
    cliente novo rende 1% adicional. Acima de R$ 100.000 de faturamento no ciclo,
    o excedente rende mais 1%, aplicado uma unica vez sobre o total.

Vendedor EXTERNO
    Base e a liquidez efetivamente paga (relatorio 733), a 7%, sem bonus.

Canal SITE
    Nao gera comissao. O faturamento e apurado apenas para acompanhamento.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from crm_npd.domain.enums import StatusManualCliente, TipoVendedor
from crm_npd.domain.valores import ExtratoComissao, LinhaComissao

#: Percentual base da comissao de vendedor interno, sobre o faturamento.
PCT_BASE_INTERNO = 1.0

#: Percentual adicional por linha de faturamento de cliente novo.
PCT_BONUS_CLIENTE_NOVO = 1.0

#: Percentual unico da comissao de vendedor externo, sobre a liquidez paga.
PCT_EXTERNO_LIQUIDEZ = 7.0

#: Faturamento no ciclo a partir do qual o excedente rende bonus de volume.
LIMITE_BONUS_VOLUME = 100_000.0

#: Percentual aplicado ao excedente acima de :data:`LIMITE_BONUS_VOLUME`.
PCT_BONUS_VOLUME = 0.01


def classificar_cliente_novo(
    *,
    primeiro_documento: str | None,
    primeira_data_iso: str | None,
    documento_atual: str,
    periodo_inicio_iso: str,
    periodo_fim_iso: str,
    is_jit: bool,
    status_manual: StatusManualCliente | None = None,
) -> bool:
    """Decide se uma linha de faturamento conta como cliente novo.

    Nao basta a primeira compra da identidade cair no periodo: a linha avaliada
    tem de ser exatamente aquele primeiro documento. Sem essa segunda condicao,
    todas as notas de um cliente que estreou no ciclo ganhariam bonus, e nao
    apenas a nota de estreia.

    A ordem de decisao importa: o override manual sobrepoe o calculo automatico,
    mas JIT anula o bonus em qualquer caso, inclusive contra override manual.

    Args:
        primeiro_documento: Numero do primeiro documento historico da identidade,
            ou None quando nao ha historico.
        primeira_data_iso: Data do primeiro documento, em ``YYYY-MM-DD``.
        documento_atual: Documento da linha sendo avaliada.
        periodo_inicio_iso: Inicio do ciclo, em ``YYYY-MM-DD``.
        periodo_fim_iso: Fim do ciclo, em ``YYYY-MM-DD``.
        is_jit: Se o documento e JIT (pedido programado de matriz).
        status_manual: Override lancado pela gestao, se houver.

    Returns:
        True quando a linha deve receber o bonus de cliente novo.
    """
    is_novo = False
    if primeiro_documento and primeira_data_iso:
        data = primeira_data_iso.strip()
        is_novo = bool(
            data
            and periodo_inicio_iso <= data <= periodo_fim_iso
            and documento_atual.strip() == primeiro_documento.strip()
        )

    if status_manual is StatusManualCliente.NOVO:
        is_novo = True
    elif status_manual is StatusManualCliente.RECOMPRA:
        is_novo = False

    # JIT entra em faturamento e em comissao normalmente, mas nunca ganha bonus
    # de novo: senao um pedido programado de matriz inflaria a comissao como se
    # fosse conquista comercial.
    if is_jit:
        is_novo = False
    return is_novo


def comissao_linha_interno(valor: float, *, is_novo: bool) -> tuple[float, float, float]:
    """Calcula a comissao de uma linha de faturamento de vendedor interno.

    Args:
        valor: Valor da linha de nota.
        is_novo: Se a linha recebe bonus de cliente novo.

    Returns:
        Tripla com percentual base, percentual de bonus e valor de comissao.
    """
    base_pct = PCT_BASE_INTERNO
    bonus_pct = PCT_BONUS_CLIENTE_NOVO if is_novo else 0.0
    comissao = float(valor) * ((base_pct + bonus_pct) / 100.0)
    return base_pct, bonus_pct, comissao


def bonus_volume(faturamento_total: float) -> float:
    """Calcula o bonus por faturamento acima do limite do ciclo.

    Incide apenas sobre o excedente, nao sobre o faturamento inteiro, e e somado
    uma unica vez ao total, nao distribuido entre as linhas.

    Args:
        faturamento_total: Faturamento do vendedor no ciclo.

    Returns:
        Valor do bonus, ou zero se o faturamento nao superou o limite.
    """
    total = float(faturamento_total or 0.0)
    if total > LIMITE_BONUS_VOLUME:
        return (total - LIMITE_BONUS_VOLUME) * PCT_BONUS_VOLUME
    return 0.0


def montar_extrato_interno(linhas: Iterable[LinhaComissao]) -> ExtratoComissao:
    """Consolida as linhas de um vendedor interno em extrato de ciclo.

    Args:
        linhas: Linhas de comissao ja classificadas e calculadas.

    Returns:
        Extrato com faturamento, comissao total, bonus de volume e contagens.
    """
    materializadas: tuple[LinhaComissao, ...] = tuple(linhas)
    faturamento = sum(linha.valor for linha in materializadas)
    comissao = sum(linha.comissao for linha in materializadas)
    extra = bonus_volume(faturamento)
    return ExtratoComissao(
        faturamento=faturamento,
        comissao=comissao + extra,
        extra_bonus=extra,
        novo_count=sum(1 for linha in materializadas if linha.is_novo),
        rep_count=sum(1 for linha in materializadas if not linha.is_novo),
        lines=materializadas,
    )


def comissao_linha_externo(valor: float) -> float:
    """Calcula a comissao de um titulo pago para vendedor externo.

    Args:
        valor: Valor liquido pago do titulo.

    Returns:
        Valor de comissao da linha.
    """
    return float(valor) * (PCT_EXTERNO_LIQUIDEZ / 100.0)


def montar_extrato_externo(linhas: Iterable[LinhaComissao], faturamento: float) -> ExtratoComissao:
    """Consolida as linhas de liquidez de um vendedor externo.

    O faturamento exibido para o externo vem da soma de liquidez do periodo e e
    recebido pronto, porque o monolito o apura em consulta separada que pode
    divergir da soma das linhas listadas.

    Args:
        linhas: Linhas de comissao sobre liquidez paga.
        faturamento: Base de liquidez do periodo.

    Returns:
        Extrato sem bonus e sem contagem de novos, que nao se aplicam ao externo.
    """
    materializadas: tuple[LinhaComissao, ...] = tuple(linhas)
    return ExtratoComissao(
        faturamento=float(faturamento or 0.0),
        comissao=sum(linha.comissao for linha in materializadas),
        extra_bonus=0.0,
        novo_count=0,
        rep_count=0,
        lines=materializadas,
    )


def extrato_vazio(faturamento: float = 0.0) -> ExtratoComissao:
    """Devolve um extrato sem comissao.

    Usado para o canal SITE, que apura faturamento mas nao gera comissao, e para
    vendedor inexistente.

    Args:
        faturamento: Faturamento a reportar, se houver.

    Returns:
        Extrato zerado, com o faturamento informado.
    """
    return ExtratoComissao(faturamento=float(faturamento or 0.0))


def base_de_calculo(tipo: TipoVendedor) -> str:
    """Informa qual relatorio do ERP serve de base para o canal.

    Args:
        tipo: Canal do vendedor.

    Returns:
        Nome da base de calculo, para exibicao e documentacao.
    """
    if tipo is TipoVendedor.EXTERNO:
        return "liquidez paga (relatorio 733)"
    if tipo is TipoVendedor.SITE:
        return "sem comissao"
    return "faturamento (relatorio 731)"


def total_comissao(linhas: Sequence[LinhaComissao]) -> float:
    """Soma a comissao de uma sequencia de linhas, sem bonus de volume.

    Args:
        linhas: Linhas de comissao.

    Returns:
        Soma das comissoes das linhas.
    """
    return sum(linha.comissao for linha in linhas)
