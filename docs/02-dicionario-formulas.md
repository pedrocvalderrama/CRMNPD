# Dicionário de fórmulas

Toda fórmula mora em `crm_npd/metrics/` como função pura, tipada, com docstring
Google. Os testes estão em `tests/unit/test_*.py`. O golden master
(`tests/golden/`) trava os números na base real.

Decisões de unificação: `docs/RECONCILIACAO_FORMULAS.md`.

## Comissão — `metrics/comissao.py`

| Canal | Base | Alíquota | Bônus |
|---|---|---|---|
| Interno | Faturamento (731) | 1% por linha | +1% na **primeira** nota do cliente novo; +1% sobre o excedente acima de R$ 100.000 no ciclo |
| Externo | Liquidez paga (733) | 7% | nenhum |
| Site | — | 0 | não gera comissão |

Cliente novo (critério A, aprovado): a linha tem de ser o primeiro documento
histórico da identidade **e** essa data cair no ciclo. JIT nunca recebe o bônus
de novo, mesmo com override manual.

## Score do cliente — `metrics/score_cliente.py`

`0,55 × frequência + 0,25 × recompra + 0,20 × valor`, depois tetos.

| Faixa | Piso | Significado |
|---|---|---|
| A+ | 85 | Elite |
| A | 70 | Muito forte |
| B | 55 | Bom |
| C | 35 | Morno |
| D | 0 | Frio |

Tetos: 1 compra → 25; intervalo > 240d → 39; > 180d → 54; > 120d → 69.

**Contagem de compras: dia único** (aprovado). Cinco linhas de nota no mesmo dia
são uma compra. Intervalos usam a mediana.

Prioridade de tarefa: só A+/A atrasado vira URGENTE.

## Recompra — `metrics/recompra.py`

- Intervalo típico = **mediana** dos dias entre compras (datas únicas).
- Atrasado quando `dias_desde_ultima > intervalo`.
- Recuperado: voltou a comprar depois de ficar parado acima do próprio intervalo,
  com pelo menos duas compras anteriores.
- Situação: NOVO / ATIVO / EM RISCO (1,3× o intervalo) / INATIVO.

## Score do vendedor — `metrics/score_vendedor.py`

Relativo à própria equipe (Interno ou Externo), nunca entre canais.

| Dimensão | Peso |
|---|---|
| Atividade (orçamentos) | 50% |
| Pipeline (valor) | 15% |
| Conversão ajustada por amostra | 10% |
| Resultado (faturamento) | 10% |
| Carteira (novo 1,0 / recuperado 0,8 / recorrente 0,3) | 10% |
| Evolução | 5% |

## Funil — `metrics/funil.py`

Etapa deduzida do relatório 104, nunca editada no CRM. Ordem de verdade:

1. Tem `pedido_id` → GANHO
2. Texto de desfecho (APROVADO / FATURADO / CANCELADO / PERDIDO)
3. Código `F` sem desfecho → ENCERRADO
4. Textos de etapa aberta
5. Default NOVO

**Conversão de proposta** (regra única): `pedido_id` não vazio **ou**
`desc_status = PEDIDO APROVADO`.

**Taxa de faturamento** (notas / pedidos) é outra métrica. Não se chama mais
"conversão" na tela de Operação.

SLA de parado: 14 dias em etapa acionável (CONTATO, NEGOCIACAO, APROVACAO).

## Períodos e percentuais

- `ciclo_faturamento(referencia, dia=24)` → 25 do mês anterior até o dia 24.
- Divisão por zero: `percentual` devolve 0; `variacao_percentual` devolve 100%
  quando a base é zero e o atual não é; `variacao_percentual_ou_nulo` devolve
  `None` (usado no score, para não pontuar o máximo por falta de base).

## Churn

Identidades que compraram no ano anterior e não voltaram no ano atual, agrupadas
por `identity_key`. Não usa `rede_key` (coluna vazia nas notas importadas).
