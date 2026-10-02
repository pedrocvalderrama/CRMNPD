"""Objetos de valor imutaveis trocados entre formulas e servicos."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from crm_npd.domain.enums import FaixaScore


@dataclass(frozen=True, slots=True)
class Periodo:
    """Intervalo fechado de datas, inclusivo nas duas pontas.

    Attributes:
        inicio: Primeiro dia do periodo.
        fim: Ultimo dia do periodo, inclusive.
    """

    inicio: date
    fim: date

    def __post_init__(self) -> None:
        """Valida a ordem das datas.

        Raises:
            ValueError: Se ``fim`` for anterior a ``inicio``.
        """
        if self.fim < self.inicio:
            raise ValueError(f"Periodo invertido: {self.inicio} ate {self.fim}")

    def contem(self, dia: date) -> bool:
        """Indica se uma data cai dentro do periodo.

        Args:
            dia: Data a testar.

        Returns:
            True se a data estiver entre inicio e fim, inclusive.
        """
        return self.inicio <= dia <= self.fim

    @property
    def dias_corridos(self) -> int:
        """Quantidade de dias corridos no periodo, contando as duas pontas."""
        return (self.fim - self.inicio).days + 1


@dataclass(frozen=True, slots=True)
class EstatisticasIdentidade:
    """Historico de compras consolidado de uma identidade comercial.

    E a entrada de todo o calculo de score e de recompra. Uma identidade agrupa
    matriz, filiais e rede sob a mesma chave comercial.

    Attributes:
        valor_total: Faturamento acumulado da identidade.
        pedidos: Numero de compras consideradas no historico.
        intervalo_mediano: Mediana de dias entre compras consecutivas, ou None
            quando nao ha historico suficiente para calcular.
        atraso_atual: Dias desde a ultima compra ate a data de referencia, ou
            None quando nao se conhece a ultima compra.
        ultima_data: Data da ultima compra, se conhecida.
    """

    valor_total: float = 0.0
    pedidos: int = 0
    intervalo_mediano: float | None = None
    atraso_atual: int | None = None
    ultima_data: date | None = None


@dataclass(frozen=True, slots=True)
class ScoreIdentidade:
    """Resultado do calculo de score de uma identidade comercial.

    Attributes:
        score: Nota final de 0 a 100, com uma casa decimal.
        faixa: Faixa de qualidade correspondente a nota.
        frequencia: Parcial da dimensao frequencia, antes dos pesos.
        recompra: Parcial da dimensao recompra, antes dos pesos.
        valor: Parcial da dimensao valor relativo, antes dos pesos.
    """

    score: float
    faixa: FaixaScore
    frequencia: float
    recompra: float
    valor: float


@dataclass(frozen=True, slots=True)
class LinhaComissao:
    """Uma linha de comissao, correspondente a uma nota ou a um titulo pago.

    Attributes:
        data: Data do documento, em ISO.
        cliente: Nome exibido do cliente.
        valor: Base de calculo da linha.
        base_pct: Percentual base aplicado.
        bonus_pct: Percentual de bonus aplicado.
        comissao: Valor de comissao da linha.
        is_novo: Indica se a linha recebeu bonus de cliente novo.
        is_jit: Indica se o documento e JIT, que nunca recebe bonus de novo.
        identity_key: Chave da identidade comercial.
        doc_ref: Numero da nota ou do pedido usado como documento de referencia.
    """

    data: str
    cliente: str
    valor: float
    base_pct: float
    bonus_pct: float
    comissao: float
    is_novo: bool = False
    is_jit: bool = False
    identity_key: str = ""
    doc_ref: str = ""


@dataclass(frozen=True, slots=True)
class ExtratoComissao:
    """Fechamento de comissao de um vendedor em um ciclo.

    Attributes:
        faturamento: Base total do periodo.
        comissao: Comissao total, incluindo o bonus de volume.
        extra_bonus: Parcela do bonus por faturamento acima do limite.
        novo_count: Linhas classificadas como cliente novo.
        rep_count: Linhas classificadas como recompra.
        lines: Detalhamento linha a linha.
    """

    faturamento: float = 0.0
    comissao: float = 0.0
    extra_bonus: float = 0.0
    novo_count: int = 0
    rep_count: int = 0
    lines: tuple[LinhaComissao, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DimensoesScoreVendedor:
    """Parciais normalizadas do score de performance de um vendedor.

    Cada dimensao vai de 0 a 100 e e normalizada contra o melhor desempenho da
    propria equipe (Interno ou Externo), nunca entre equipes diferentes.

    Attributes:
        atividade: Volume de orcamentos abertos.
        pipeline: Valor de orcamentos abertos, com amortecimento por raiz.
        conversao: Taxa de ganho, ajustada por tamanho de amostra.
        resultado: Faturamento, com amortecimento por raiz.
        carteira: Qualidade da carteira conquistada no periodo.
        evolucao: Momento em relacao ao periodo anterior.
    """

    atividade: float = 0.0
    pipeline: float = 0.0
    conversao: float = 0.0
    resultado: float = 0.0
    carteira: float = 0.0
    evolucao: float = 0.0


@dataclass(frozen=True, slots=True)
class MetricasPipeline:
    """Resumo do funil comercial em um periodo.

    Attributes:
        ativos: Orcamentos ainda no pipeline.
        pipeline: Soma do valor dos orcamentos ativos.
        ponderado: Soma do valor ativo multiplicado pela probabilidade da etapa.
        ganhos: Orcamentos com desfecho de ganho.
        fechados: Orcamentos com desfecho conhecido (ganho ou perdido).
        conversao: Percentual de ganho sobre os fechados.
        acionaveis: Orcamentos em etapa que admite acao comercial.
        parados: Acionaveis que estouraram o SLA de dias em aberto.
    """

    ativos: int = 0
    pipeline: float = 0.0
    ponderado: float = 0.0
    ganhos: int = 0
    fechados: int = 0
    conversao: float = 0.0
    acionaveis: int = 0
    parados: int = 0


@dataclass(frozen=True, slots=True)
class PainelMeta:
    """Acompanhamento de meta de um vendedor ou da empresa.

    Attributes:
        pct: Percentual atingido da meta.
        falta: Valor que ainda falta, nunca negativo.
        por_dia: Valor necessario por dia util restante.
    """

    pct: float = 0.0
    falta: float = 0.0
    por_dia: float = 0.0
