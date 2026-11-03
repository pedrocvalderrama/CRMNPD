"""Formulas de comissao.

E o modulo de maior risco financeiro do sistema: qualquer alteracao aqui muda
dinheiro pago a vendedor. Toda regra esta documentada e coberta por teste.

Regras vigentes, extraidas de ``compute_vendor_commission`` e
``linhas_comissao_externo_liquidez`` do monolito:

Vendedor INTERNO
    Ciclo 26→25 (fechamento dia 25). Base e o faturamento liquido atribuido ao
    vendedor (NF de saida + DEMO - devolucoes, sem frete e sem cliche).
    Devolucoes descontam da base de faturamento (inclusive para meta e bonus
    acima de R$ 100 mil) e da comissao, aplicando a mesma aliquota do pedido/ciclo.

    Toda linha valida rende 1%. Se o Sisplan marcou o pedido como cliente
    novo (MOTIVO 10 — PEDIDO PRIMEIRA COMPRA) ou pedido recuperado (MOTIVO 11 —
    PEDIDO RECUPERADO ARTE NOVA; MOTIVO 13 — PEDIDO RECUPERADO ARTE REPETICAO),
    todas as notas daquele pedido rendem +1% (2% no total) e entram nos
    relatorios como pedido novo. No extrato de comissao e no Excel, os motivos
    11 e 13 exibem a tag RECUPERADO mantendo os mesmos 2%.
    JIT IMEDIATO (05) anula o bonus de novo, mesmo se o Sisplan disser que e
    cliente novo/recuperado, e mesmo com override manual.

    A meta e de faturamento do mesmo ciclo da comissao (setembro/2026 =
    26/08 a 25/09). Enquanto nao bate: +1% uma vez sobre o que passar de
    R$ 100 mil. Se bater, o bonus de volume cai; todas as linhas passam a
    1,75% e os pedidos novos/recuperados (10, 11 e 13) continuam em 2%.

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

#: Percentual de todas as linhas depois que a vendedora bate a meta.
PCT_BASE_INTERNO_META = 1.75

#: Percentual total das notas de pedido novo ou recuperado (Sisplan MOTIVOS 10, 11 e 13).
PCT_NOVO = 2.0

#: Percentual adicional por linha de faturamento de cliente novo (antes da meta).
PCT_BONUS_CLIENTE_NOVO = 1.0

#: Percentual unico da comissao de vendedor externo, sobre a liquidez paga.
PCT_EXTERNO_LIQUIDEZ = 7.0

#: Faturamento no ciclo a partir do qual o excedente rende bonus de volume.
LIMITE_BONUS_VOLUME = 100_000.0

#: Percentual aplicado ao excedente acima de :data:`LIMITE_BONUS_VOLUME`.
PCT_BONUS_VOLUME = 0.01

#: Codigo Sisplan em PEDIDO_001.MOTIVO / MENSAGEM_001: PEDIDO PRIMEIRA COMPRA.
MOTIVO_SISPLAN_PRIMEIRA_COMPRA = "10"

#: Codigo Sisplan em PEDIDO_001.MOTIVO / MENSAGEM_001: PEDIDO RECUPERADO ARTE NOVA.
MOTIVO_SISPLAN_RECUPERADO_ARTE_NOVA = "11"

#: Codigo Sisplan em PEDIDO_001.MOTIVO / MENSAGEM_001: PEDIDO RECUPERADO ARTE REPETICAO.
MOTIVO_SISPLAN_RECUPERADO_ARTE_REPETICAO = "13"

#: Codigos Sisplan de pedido recuperado que rendem 2% e contam como novo no CRM.
MOTIVOS_SISPLAN_RECUPERADO = frozenset({
    MOTIVO_SISPLAN_RECUPERADO_ARTE_NOVA,
    MOTIVO_SISPLAN_RECUPERADO_ARTE_REPETICAO,
})

#: Todos os codigos Sisplan que contam como pedido novo e rendem 2% na comissao.
MOTIVOS_SISPLAN_NOVO = frozenset({
    MOTIVO_SISPLAN_PRIMEIRA_COMPRA,
    *MOTIVOS_SISPLAN_RECUPERADO,
})


def _texto_upper(valor: str | None) -> str:
    return (valor or "").strip().upper()


def _compactar(valor: str) -> str:
    return "".join(ch for ch in valor if not ch.isspace())


def _codigo_motivo(motivo: str | None) -> str:
    texto = _texto_upper(motivo)
    if not texto:
        return ""
    token = texto.split()[0]
    return token.split("-", 1)[0]


def pedido_sisplan_recuperado(
    motivo: str | None,
    desc_motivo: str | None = None,
) -> bool:
    """True se o Sisplan marcou o pedido como recuperado (codigos 11 ou 13).

    Abrange ``11 - PEDIDO RECUPERADO ARTE NOVA`` e
    ``13 - PEDIDO RECUPERADO ARTE REPETICAO``. Esses pedidos recebem os mesmos
    2% de comissao e contam como novos no CRM, mas sao identificados com a tag
    ``RECUPERADO`` no extrato de comissao e no Excel.

    Args:
        motivo: ``PEDIDO_001.MOTIVO`` ou o texto concatenado.
        desc_motivo: Descricao em ``MENSAGEM_001``, se vier separado.

    Returns:
        True quando o pedido tem motivo 11 ou 13 (pedido recuperado) no Sisplan.
    """
    if _codigo_motivo(motivo) in MOTIVOS_SISPLAN_RECUPERADO:
        return True
    blob = " ".join(
        parte for parte in (_texto_upper(motivo), _texto_upper(desc_motivo)) if parte
    )
    return "PEDIDO RECUPERADO" in blob


def pedido_sisplan_cliente_novo(
    motivo: str | None,
    desc_motivo: str | None = None,
) -> bool:
    """True se o Sisplan marcou o pedido com motivo que conta como novo (2%).

    A fonte oficial e o MOTIVO do pedido:
    - ``10`` — ``PEDIDO PRIMEIRA COMPRA``
    - ``11`` — ``PEDIDO RECUPERADO ARTE NOVA``
    - ``13`` — ``PEDIDO RECUPERADO ARTE REPETICAO``

    Todas as notas daquele pedido herdam o flag.

    Args:
        motivo: ``PEDIDO_001.MOTIVO`` ou o texto ja concatenado
            (ex.: ``10 - PEDIDO PRIMEIRA COMPRA``).
        desc_motivo: Descricao em ``MENSAGEM_001``, se vier separado.

    Returns:
        True quando o pedido conta como novo no Sisplan (motivos 10, 11 ou 13).
    """
    if _codigo_motivo(motivo) in MOTIVOS_SISPLAN_NOVO:
        return True
    if pedido_sisplan_recuperado(motivo, desc_motivo):
        return True
    blob = " ".join(
        parte for parte in (_texto_upper(motivo), _texto_upper(desc_motivo)) if parte
    )
    return "PRIMEIRA COMPRA" in blob or "CLIENTE NOVO" in blob


def condicao_jit_imediato(condicao: str | None) -> bool:
    """True se a condicao de entrega e ``05 - JIT IMEDIATO``.

    So este JIT anula o bonus de cliente novo. ``98 - JIT`` e outros JIT
    nao disparam a excecao.

    Args:
        condicao: ``PEDIDO_001.TIPO_CONDICAO`` / ``orders.condicao_entrega``.

    Returns:
        True quando a condicao e JIT IMEDIATO.
    """
    compacto = _compactar(_texto_upper(condicao))
    return compacto.startswith("05") and "JIT" in compacto and "IMEDIATO" in compacto


def invoice_eh_devolucao(*, tipo_item_ref: str | None = None, valor: float = 0.0) -> bool:
    """True se a linha de faturamento e devolucao.

    Devolucoes entram com valor negativo descontando da base do ciclo e da
    comissao, pela mesma regra de aliquota do pedido/ciclo.

    Args:
        tipo_item_ref: Natureza gravada na invoice (``D`` para devolucao).
        valor: Valor da linha; negativo tambem conta como devolucao.

    Returns:
        True quando a linha representa devolucao.
    """
    if _texto_upper(tipo_item_ref) == "D":
        return True
    return float(valor or 0.0) < 0.0


def valor_assinado_faturamento(*, tipo_item_ref: str | None = None, valor: float = 0.0) -> float:
    """Garante valor negativo quando a linha for devolucao.

    No sync do Firebird as devolucoes ja entram negativas em ``invoices``. Esta
    funcao protege contra importacao avulsa em que ``tipo_item_ref='D'`` venha
    com sinal positivo, garantindo que devolucao sempre desconte.

    Args:
        tipo_item_ref: Natureza gravada na invoice (``D`` para devolucao).
        valor: Valor bruto lido da linha.

    Returns:
        Valor assinado (negativo se devolucao, original caso contrario).
    """
    v = float(valor or 0.0)
    if invoice_eh_devolucao(tipo_item_ref=tipo_item_ref, valor=v):
        return -abs(v)
    return v


def status_manual_de_texto(valor: str | None) -> StatusManualCliente | None:
    """Converte o texto gravado em ``customer_status_overrides`` no enum.

    Args:
        valor: ``NOVO``, ``RECOMPRA`` ou vazio.

    Returns:
        Enum correspondente, ou None se nao houver override.
    """
    texto = _texto_upper(valor)
    if texto == StatusManualCliente.NOVO:
        return StatusManualCliente.NOVO
    if texto == StatusManualCliente.RECOMPRA:
        return StatusManualCliente.RECOMPRA
    return None


def classificar_cliente_novo(
    *,
    pedido_sisplan_novo: bool,
    is_jit_imediato: bool,
    status_manual: StatusManualCliente | None = None,
) -> bool:
    """Decide se uma linha de faturamento conta como pedido novo (2%).

    Fonte automatica: o Sisplan marca cliente novo/recuperado no **pedido**
    (MOTIVOS 10, 11 e 13). Todas as notas daquele pedido herdam o bonus. O
    criterio antigo (primeira nota historica da identidade) nao se aplica mais.

    A ordem de decisao importa: o override manual sobrepoe o Sisplan, mas
    JIT IMEDIATO anula o bonus em qualquer caso, inclusive contra override.

    Args:
        pedido_sisplan_novo: Se o pedido vinculado esta marcado com motivo 10,
            11 ou 13 no Sisplan.
        is_jit_imediato: Se a origem e ``05 - JIT IMEDIATO``.
        status_manual: Override lancado pela gestao, se houver.

    Returns:
        True quando a linha deve receber o bonus de 2% de pedido novo.
    """
    is_novo = bool(pedido_sisplan_novo)

    if status_manual is StatusManualCliente.NOVO:
        is_novo = True
    elif status_manual is StatusManualCliente.RECOMPRA:
        is_novo = False

    if is_jit_imediato:
        is_novo = False
    return is_novo


def rotulo_status_linha(
    *,
    is_devolucao: bool = False,
    is_novo: bool = False,
    is_recuperado: bool = False,
) -> str:
    """Devolve o rotulo exibido no extrato de comissao e na exportacao Excel.

    Args:
        is_devolucao: Se a linha e devolucao.
        is_novo: Se a linha recebe a aliquota de 2% (novo ou recuperado 11/13).
        is_recuperado: Se o pedido veio com motivo 11 ou 13 no Sisplan.

    Returns:
        ``DEVOLUÇÃO``, ``RECUPERADO``, ``NOVO`` ou ``CARTEIRA``.
    """
    if is_devolucao:
        return "DEVOLUÇÃO"
    if is_novo and is_recuperado:
        return "RECUPERADO"
    if is_novo:
        return "NOVO"
    return "CARTEIRA"


def bateu_meta_faturamento(faturamento: float, meta: float) -> bool:
    """True se o faturamento liquido do ciclo atingiu a meta do vendedor.

    A meta lancada para um mes (ex.: setembro/2026) compara com o mesmo
    numero da comissao: faturamento liquido de 26/08 a 25/09 (ja descontando
    devolucoes). Meta zero ou negativa nao conta como batida.

    Args:
        faturamento: Base liquida de comissao do ciclo (NF saida + DEMO - D).
        meta: Meta de faturamento cadastrada para o mes do ciclo.

    Returns:
        True quando ha meta e o faturamento e maior ou igual a ela.
    """
    meta_v = float(meta or 0.0)
    return meta_v > 0 and float(faturamento or 0.0) >= meta_v


def comissao_linha_interno(
    valor: float,
    *,
    is_novo: bool,
    bateu_meta: bool = False,
) -> tuple[float, float, float]:
    """Calcula a comissao de uma linha de faturamento de vendedor interno.

    Aceita valores positivos (vendas/DEMO) e negativos (devolucoes), aplicando a
    mesma regra de aliquota para que devolucoes estornem proporcionalmente.

    Args:
        valor: Valor assinado da linha (negativo em devolucoes).
        is_novo: Se a linha segue a aliquota de 2% (pedido 10, 11 ou 13).
        bateu_meta: Se a vendedora atingiu a meta de faturamento liquido do ciclo.

    Returns:
        Tripla com percentual base, percentual de bonus e valor de comissao.
    """
    if is_novo:
        pct = PCT_NOVO
        base_pct = PCT_BASE_INTERNO_META if bateu_meta else PCT_BASE_INTERNO
        bonus_pct = pct - base_pct
    elif bateu_meta:
        pct = PCT_BASE_INTERNO_META
        base_pct = pct
        bonus_pct = 0.0
    else:
        pct = PCT_BASE_INTERNO
        base_pct = pct
        bonus_pct = 0.0
    comissao = float(valor) * (pct / 100.0)
    return base_pct, bonus_pct, comissao


def bonus_volume(faturamento_total: float, *, bateu_meta: bool = False) -> float:
    """Calcula o bonus por faturamento liquido acima do limite do ciclo.

    Incide apenas sobre o excedente, nao sobre o faturamento inteiro, e e
    somado uma unica vez ao total, nao distribuido entre as linhas. Cai
    inteiro quando a vendedora bate a meta: nesse caso a aliquota de todas
    as linhas ja sobe para 1,75%.

    Args:
        faturamento_total: Faturamento liquido do vendedor no ciclo.
        bateu_meta: Se True, devolve zero.

    Returns:
        Valor do bonus, ou zero se o faturamento nao superou o limite.
    """
    if bateu_meta:
        return 0.0
    total = float(faturamento_total or 0.0)
    if total > LIMITE_BONUS_VOLUME:
        return (total - LIMITE_BONUS_VOLUME) * PCT_BONUS_VOLUME
    return 0.0


def montar_extrato_interno(
    linhas: Iterable[LinhaComissao],
    *,
    bateu_meta: bool = False,
) -> ExtratoComissao:
    """Consolida as linhas de um vendedor interno em extrato de ciclo.

    Linhas de devolucao entram com valor e comissao negativos, descontando tanto
    da base de faturamento (incluindo o calculo de bonus > R$ 100 mil) quanto da
    comissao final, sem inflar a contagem de vendas (``novo_count`` / ``rep_count``).

    Args:
        linhas: Linhas de comissao ja classificadas e calculadas.
        bateu_meta: Se a vendedora atingiu a meta; desliga o bonus de volume.

    Returns:
        Extrato com faturamento liquido, comissao liquida, bonus e contagens.
    """
    materializadas: tuple[LinhaComissao, ...] = tuple(linhas)
    saidas = tuple(linha for linha in materializadas if not linha.is_devolucao)
    faturamento = sum(linha.valor for linha in materializadas)
    comissao = sum(linha.comissao for linha in materializadas)
    extra = bonus_volume(faturamento, bateu_meta=bateu_meta)
    return ExtratoComissao(
        faturamento=faturamento,
        comissao=comissao + extra,
        extra_bonus=extra,
        novo_count=sum(1 for linha in saidas if linha.is_novo),
        rep_count=sum(1 for linha in saidas if not linha.is_novo),
        lines=materializadas,
        bateu_meta=bateu_meta,
        devolucoes_valor=sum(linha.valor for linha in materializadas if linha.is_devolucao),
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
    return "faturamento liquido (NF saida + DEMO - devolucoes, sem frete e sem cliche)"


def total_comissao(linhas: Sequence[LinhaComissao]) -> float:
    """Soma a comissao de uma sequencia de linhas, sem bonus de volume.

    Args:
        linhas: Linhas de comissao.

    Returns:
        Soma das comissoes das linhas.
    """
    return sum(linha.comissao for linha in linhas)
