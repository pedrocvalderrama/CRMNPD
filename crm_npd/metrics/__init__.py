"""Formulas de negocio do CRM NPD: funcoes puras, tipadas e testaveis.

Esta e a camada mais protegida do projeto. Nenhum modulo aqui pode importar
``sqlite3``, ``flask`` ou ``fdb``: a regra e imposta por lint (TID251 em
``pyproject.toml``) e existe porque o monolito chegou a ter calculo de datas
abrindo conexao com o banco.

Consequencia pratica: toda formula pode ser verificada com numeros na mao, sem
banco, sem servidor e sem dado real de cliente.

Organizacao:

``comissao``
    Comissao interna (faturamento), externa (liquidez) e bonus de volume.
``score_cliente``
    Score de recompra por identidade e prioridade de tarefa.
``score_vendedor``
    Score mensal de performance, relativo a equipe.
``recompra``
    Intervalos, atraso, situacao do cliente e cliente recuperado.
``funil``
    Etapa do funil a partir do 104, probabilidade e metricas de pipeline.
``periodos``
    Ciclo de faturamento, mes calendario e dias uteis.
``percentuais``
    Percentuais, variacoes, projecao e painel de meta.
"""

from crm_npd.metrics import (
    comissao,
    funil,
    percentuais,
    periodos,
    recompra,
    score_cliente,
    score_vendedor,
)

__all__ = [
    "comissao",
    "funil",
    "percentuais",
    "periodos",
    "recompra",
    "score_cliente",
    "score_vendedor",
]
