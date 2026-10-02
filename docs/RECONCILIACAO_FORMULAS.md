# Reconciliação de fórmulas divergentes

> **Status: aguardando decisão.** Nenhuma unificação foi aplicada. O código continua
> calculando exatamente como calculava antes, e o golden master comprova isso.

O monolito calcula a mesma métrica de formas diferentes em lugares diferentes. Durante a
extração das fórmulas para `crm_npd/metrics/`, cada versão foi preservada como estava,
porque escolher uma delas muda números que a empresa já usa — inclusive comissão paga.

Este documento mede o tamanho de cada divergência na base real para que a decisão seja
tomada com números. Todos os valores são reproduzíveis com:

```
.venv\Scripts\python.exe scripts/reconciliacao_divergencias.py --ciclo 2026-09
```

O script roda em modo somente leitura sobre `tests/golden/fixture/baseline.db`, a base
congelada do golden master. Data de referência: **2026-10-01**. Ciclo de faturamento
analisado: **25/08/2026 a 24/09/2026**.

---

## Resumo

| # | Métrica | Divergência | Impacto medido | Risco |
|---|---|---|---|---|
| 1 | Cliente NOVO | data + documento vs apenas data | R$ 508,89 por ciclo de bônus (~R$ 6.106/ano) | **Dinheiro** |
| 2 | Mix Novo/Recorrente | por pedido vs por identidade | 316 vs 190 (1,66x) | Confiança |
| 3 | Intervalo de recompra | mediana vs média | 56 de 2.183 clientes (2,6%) com atraso divergente | Operação |
| 4 | Compras no score | linha de NF vs dia único | **1.231 de 4.841 identidades (25,4%) trocam de faixa** | **Operação** |
| 5 | Taxa de conversão | 4 implementações | 0,58 p.p. entre duas; a quarta mede outra coisa | Confiança |
| 6 | Churn | `rede_key` vs `identity_key` | 865 vs 778 (87 clientes) | Confiança |

A divergência 4 é de longe a maior e é a que eu recomendo tratar primeiro.

---

## 1. Critério de "cliente NOVO"

**Onde divergem**

- **A — data + documento:** `compute_vendor_commission` (comissão) e
  `crm_npd/services/performance.py` (mix). Marca NOVO apenas a linha cujo documento é
  exatamente o primeiro faturamento histórico da identidade.
- **B — apenas data:** `summarize_team_status_by_orders` e o mix por identidade. Marca
  NOVO qualquer linha de uma identidade cujo primeiro faturamento histórico caiu dentro
  do ciclo.

**Medição no ciclo 25/08 a 24/09/2026**

| | A (data+doc) | B (só data) |
|---|---|---|
| Linhas de NF marcadas NOVO | 42 | 94 |
| Identidades NOVO | 33 | 33 |
| Base faturada marcada NOVO | R$ 84.023,11 | R$ 134.911,71 |
| Bônus de 1% sobre essa base | R$ 840,23 | R$ 1.349,12 |

O conjunto de clientes novos é **idêntico** nos dois critérios: as mesmas 33 identidades.
A divergência é sobre **quantas notas daquele cliente novo recebem o bônus de 1%**. Em A,
só a primeira; em B, todas as do ciclo.

**Diferença: R$ 508,89 por ciclo, cerca de R$ 6.106 por ano.**

**Efeito colateral já presente hoje:** em `performance.py`, `novo_ids` e `rep_ids` são
conjuntos independentes. 6 das 33 identidades entram nos **dois**, porque a primeira nota
é NOVO e as seguintes são recorrentes. Resultado: `novo_count + rep_count` soma 39 onde
existem 33 clientes. Isso infla a dimensão "carteira", que vale 10% do score do vendedor.

**Pergunta de negócio:** o bônus de cliente novo é um prêmio pela **conquista** (paga uma
vez, critério A) ou pelo **faturamento do primeiro ciclo** do cliente (critério B)?

---

## 2. Unidade de contagem do mix Novo/Recorrente

**Onde divergem**

- **A — por pedido único:** `summarize_team_status_by_orders`.
- **B — por identidade comercial:** mix exibido na Visão do Vendedor.

**Medição no mesmo ciclo**

| | Valor |
|---|---|
| Pedidos únicos | 316 |
| Identidades | 190 |
| Pedidos por identidade (média) | 1,66 |
| Concentração | `MATRIZ:5530` com 23 pedidos, `MATRIZ:5569` com 19, `REDE:sushiaki` com 17 |

Um cliente novo com 4 pedidos no ciclo conta **4** em A e **1** em B. Como as duas
contagens aparecem em telas diferentes com o mesmo rótulo, o vendedor vê "X novos" com
dois valores distintos dependendo de onde olha.

**Recomendação:** padronizar em identidade e manter a contagem por pedido apenas onde a
pergunta é operacional ("quantos pedidos faturar"), com rótulo explícito.

---

## 3. Intervalo de recompra: mediana ou média

**Onde divergem**

- **A — mediana:** `calcular_metricas_recompra`, score do cliente, classificação
  RECUPERADO.
- **B — média:** `_build_repurchase_map`, que alimenta a coluna "atrasado" da lista de
  clientes.

**Medição sobre todo o histórico**

| | Valor |
|---|---|
| Identidades com 2+ compras | 2.183 |
| Classificação de atraso divergente | 56 (2,6%) |
| Atrasado só pela mediana | 47 |
| Atrasado só pela média | 9 |
| Média − mediana (mediana das diferenças) | +0,0 dias |
| Média − mediana (maior diferença) | +501,3 dias |

Na maioria dos clientes as duas medidas coincidem. A cauda é o problema: um cliente que
ficou 501 dias sem comprar e depois voltou ao ritmo normal tem a média destruída e sai da
lista de atrasados pela regra B, enquanto a regra A continua cobrando.

**Recomendação técnica: mediana.** É resistente a esse tipo de outlier, que é comum em
cliente sazonal, e já é a regra usada no score e no RECUPERADO. Padronizar em mediana
muda a coluna "atrasado" de 56 clientes.

---

## 4. Contagem de compras no score do cliente (maior divergência)

**Onde divergem**

- **A — linha de nota:** `_build_score_map` faz `pedidos += 1` por linha de NF.
- **B — dia único:** `_build_repurchase_map` usa `qtd = len(dias)`.

A granularidade entra em **duas pontas** do score, e é por isso que o efeito é grande:

1. no número de compras, que define o teto de compra única e a dimensão frequência;
2. nos intervalos entre compras — várias linhas no mesmo dia geram intervalos de **zero
   dias**, o que derruba a mediana e faz o cliente parecer muito mais frequente do que é.

**Medição sobre todas as identidades**

| | Valor |
|---|---|
| Identidades avaliadas | 4.841 |
| Score diferente entre A e B | 1.568 |
| **Faixa (A+/A/B/C/D) diferente entre A e B** | **1.231 (25,4%)** |
| Maior divergência observada | A = 64,9 e B = 8,1 (`MATRIZ:3543`, `CLIENTE:887` e outros) |

Um cliente que comprou **uma única vez**, em 5 linhas de nota no mesmo dia, pontua **64,9
(faixa B)** na regra A e **8,1 (faixa D)** na regra B. A regra A o trata como alguém que
comprou 5 vezes com intervalo zero entre as compras.

**Recomendação técnica: dia único (B).** Nota fiscal é documento contábil, não evento de
compra; cinco linhas no mesmo dia são uma compra. A regra A é, na prática, um defeito de
cálculo, e é a que está alimentando o score exibido hoje.

**Atenção:** essa é a correção que mais muda a tela. Um quarto da base troca de faixa, e
faixa dispara prioridade de tarefa. Se aprovada, deve ir junto com um aviso à equipe
comercial, porque clientes hoje marcados como B vão aparecer como D.

---

## 5. Taxa de conversão: quatro fórmulas com o mesmo nome

**Medição no acumulado de 2026 (01/01 a 01/10), 1.541 orçamentos**

| Implementação | Onde | Resultado |
|---|---|---|
| A | Estudo de Mercado: `pedido_id <> ''` **ou** `desc_status = 'PEDIDO APROVADO'` | 58,08% (895/1541) |
| B | Performance: `_sisplan_stage(...) == 'GANHO'` | 58,08% (895/1541) |
| C | `metrics_orc`: `pedido_id` fora de `('', '0', 'None')` | 57,50% (886/1541) |
| D | Operação: NFs ÷ pedidos | 82,70% (3768/4556) |

A e B coincidem no acumulado — são a mesma regra escrita duas vezes, e consolidar as duas
em `metrics/funil.py` não muda número nenhum. C difere em 9 orçamentos (0,58 p.p.), que
são registros com `pedido_id` gravado como `'0'` ou `'None'` em vez de vazio: lixo de
importação, não regra de negócio.

**D não é taxa de conversão.** É a relação pedido → faturamento, uma métrica operacional
legítima com o rótulo errado na tela ("CONVERSÃO QTD"). Isso é o mais perigoso dos quatro,
porque alguém pode comparar 82,70% com 58,08% e concluir algo falso.

**Recomendação:** adotar A/B como conversão de proposta, corrigir C limpando `'0'`/`'None'`
na importação, e renomear D para "Taxa de faturamento de pedidos".

---

## 6. Chave de agrupamento do churn

**Onde divergem**

- **Atual:** `COALESCE(NULLIF(TRIM(rede_key),''), 'CID:'||cliente_id, 'N:'||nome)` no
  Estudo de Mercado.
- **Resto do CRM:** `identity_key` de `customer_identity_rules`, com a hierarquia
  GRUPO > REDE > CLIENTE.

**Medição: comprou em 2025 e não voltou de 01/01/2026 a 01/10/2026**

| | Valor |
|---|---|
| Churn com `rede_key` (atual) | 865 |
| Churn com `identity_key` | 778 |
| Diferença | 87 clientes |
| **NFs com `rede_key` preenchida** | **0 de 9.134** |

O achado relevante não é a diferença de 87: é que a coluna `invoices.rede_key` está
**vazia em todas as 9.134 notas** do recorte. O `COALESCE` nunca encontra `rede_key` e
sempre cai no fallback `CID:cliente_id`. Ou seja, o churn agrupa por cliente bruto e
**ignora por completo** o sistema de identidade comercial do CRM: matriz e filiais da mesma
rede são contadas como perdas separadas.

**Recomendação: trocar por `identity_key`.** O número de churn cai de 865 para 778 e passa
a significar a mesma coisa que "cliente" significa em todas as outras telas.

---

## Decisões necessárias

| # | Decisão | Minha recomendação | Quem decide |
|---|---|---|---|
| 1 | Bônus de cliente novo: uma vez (A) ou todo o primeiro ciclo (B) | Manter A, e corrigir a dupla contagem em `novo_ids`/`rep_ids` | **Negócio** — mexe em comissão |
| 2 | Unidade do mix | Identidade, com rótulo explícito onde for por pedido | Negócio |
| 3 | Intervalo de recompra | Mediana | Técnico, com aviso |
| 4 | Compras no score | Dia único | Técnico, **com aviso à equipe** |
| 5 | Conversão | A/B como padrão; renomear D | Técnico |
| 6 | Churn | `identity_key` | Técnico |

Nenhuma dessas mudanças será aplicada sem aprovação. Quando uma for aprovada, o
procedimento é: alterar a função pura em `crm_npd/metrics/`, rodar o golden master,
conferir que **apenas** os números daquela regra mudaram, e regravar o baseline
registrando a decisão no commit.
