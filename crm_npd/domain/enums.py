"""Enumeracoes do dominio.

Os valores textuais sao exatamente os que o monolito ja gravava e exibia. Trocar
qualquer string aqui muda tela, filtro e relatorio: sao contrato, nao detalhe.
"""

from __future__ import annotations

from enum import StrEnum


class FaixaScore(StrEnum):
    """Faixa de qualidade comercial de uma identidade de cliente."""

    ELITE = "A+"
    MUITO_FORTE = "A"
    BOM = "B"
    MORNO = "C"
    FRIO = "D"

    @property
    def label(self) -> str:
        """Texto exibido ao usuario para a faixa."""
        return _LABEL_FAIXA[self]

    @property
    def cor(self) -> str:
        """Nome de cor Bootstrap associado a faixa."""
        return _COR_FAIXA[self]


_LABEL_FAIXA: dict[FaixaScore, str] = {
    FaixaScore.ELITE: "Elite",
    FaixaScore.MUITO_FORTE: "Muito forte",
    FaixaScore.BOM: "Bom",
    FaixaScore.MORNO: "Morno",
    FaixaScore.FRIO: "Frio",
}

_COR_FAIXA: dict[FaixaScore, str] = {
    FaixaScore.ELITE: "success",
    FaixaScore.MUITO_FORTE: "primary",
    FaixaScore.BOM: "info",
    FaixaScore.MORNO: "warning",
    FaixaScore.FRIO: "secondary",
}


class PrioridadeTarefa(StrEnum):
    """Prioridade de uma tarefa de recompra."""

    URGENTE = "URGENTE"
    ALTA = "ALTA"
    MEDIA = "MÉDIA"
    BAIXA = "BAIXA"

    @property
    def ordem(self) -> int:
        """Ordem de exibicao: menor vem primeiro na lista de tarefas."""
        return _ORDEM_PRIORIDADE[self]

    @property
    def cor(self) -> str:
        """Nome de cor Bootstrap associado a prioridade."""
        return _COR_PRIORIDADE[self]


_ORDEM_PRIORIDADE: dict[PrioridadeTarefa, int] = {
    PrioridadeTarefa.URGENTE: 0,
    PrioridadeTarefa.ALTA: 1,
    PrioridadeTarefa.MEDIA: 2,
    PrioridadeTarefa.BAIXA: 3,
}

_COR_PRIORIDADE: dict[PrioridadeTarefa, str] = {
    PrioridadeTarefa.URGENTE: "danger",
    PrioridadeTarefa.ALTA: "warning",
    PrioridadeTarefa.MEDIA: "info",
    PrioridadeTarefa.BAIXA: "secondary",
}


class EtapaFunil(StrEnum):
    """Etapa do funil comercial derivada do relatorio 104 do Sisplan.

    O funil e somente leitura: a etapa e sempre deduzida do status atual do
    orcamento no ERP, nunca editada no CRM.
    """

    NOVO = "NOVO"
    CONTATO = "CONTATO"
    NEGOCIACAO = "NEGOCIACAO"
    APROVACAO = "APROVACAO"
    GANHO = "GANHO"
    PERDIDO = "PERDIDO"
    ENCERRADO = "ENCERRADO"

    @property
    def probabilidade(self) -> int:
        """Probabilidade de fechamento associada a etapa, em pontos percentuais."""
        return _PROBABILIDADE_ETAPA[self]

    @property
    def e_final(self) -> bool:
        """Indica se a etapa tira o orcamento do pipeline ativo."""
        return self in _ETAPAS_FINAIS

    @property
    def e_acionavel(self) -> bool:
        """Indica se a etapa admite acao comercial e pode ficar parada (SLA)."""
        return self in _ETAPAS_ACIONAVEIS


_PROBABILIDADE_ETAPA: dict[EtapaFunil, int] = {
    EtapaFunil.NOVO: 15,
    EtapaFunil.CONTATO: 30,
    EtapaFunil.NEGOCIACAO: 60,
    EtapaFunil.APROVACAO: 80,
    EtapaFunil.GANHO: 100,
    EtapaFunil.PERDIDO: 0,
    EtapaFunil.ENCERRADO: 0,
}

_ETAPAS_FINAIS: frozenset[EtapaFunil] = frozenset(
    {EtapaFunil.GANHO, EtapaFunil.PERDIDO, EtapaFunil.ENCERRADO}
)

_ETAPAS_ACIONAVEIS: frozenset[EtapaFunil] = frozenset(
    {EtapaFunil.CONTATO, EtapaFunil.NEGOCIACAO, EtapaFunil.APROVACAO}
)


class ClassificacaoCliente(StrEnum):
    """Situacao de recompra de um cliente em relacao ao proprio padrao."""

    SEM_HISTORICO = "SEM HISTÓRICO"
    NOVO = "NOVO"
    ATIVO = "ATIVO"
    EM_RISCO = "EM RISCO"
    INATIVO = "INATIVO"


class TipoVendedor(StrEnum):
    """Canal do vendedor, que determina a base de calculo da comissao."""

    INTERNO = "Interno"
    EXTERNO = "Externo"
    SITE = "Site"


class StatusManualCliente(StrEnum):
    """Override manual de status, gravado em ``customer_status_overrides``.

    Existe para o gestor corrigir o flag do Sisplan (MOTIVO 10) em casos
    pontuais: forcar NOVO ou RECOMPRA. JIT IMEDIATO ainda anula o bonus.
    """

    NOVO = "NOVO"
    RECOMPRA = "RECOMPRA"
