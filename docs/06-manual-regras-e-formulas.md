# Manual de regras de negócio e fórmulas

Este texto descreve **tudo o que o CRM calcula** e **onde alterar**. As fórmulas
puras (sem banco, sem tela) ficam em `crm_npd/metrics/`. Quem busca dado no
SQLite ou aplica a fórmula numa tela ainda mora em `legacy_monolith.py` e em
`crm_npd/services/`.

Como revisar com segurança:

1. Mude a constante ou a função em `crm_npd/metrics/` (ou a regra SQL no monolito).
2. Rode `.\.venv\Scripts\python.exe -m pytest tests/unit -q`.
3. Se a mudança for de número que a empresa já usa, rode também `tests/golden`
   e só regrave o baseline se a alteração for intencional.

Dicionário curto: `docs/02-dicionario-formulas.md`.
Decisões de unificação (out/2026): `docs/RECONCILIACAO_FORMULAS.md`.

```
Sisplan FDB (somente SELECT)
        │  sync/etl.py
        ▼
SQLite  orders / invoices / orcamentos / liquidez
        │
        ├── metrics/     fórmulas (o que mudar para alterar cálculo)
        ├── services/    orquestra fórmula + dado
        └── monolito     telas, SQL de filtro, identidade, metas
```

---

## 1. Onde mexer

| Quero mudar… | Arquivo principal | Teste |
|---|---|---|
| % de comissão, limite de bônus, cliente novo | `crm_npd/metrics/comissao.py` | `tests/unit/test_comissao.py` |
| Score do cliente, faixas, tetos, urgência | `crm_npd/metrics/score_cliente.py` | `tests/unit/test_score_cliente.py` |
| Intervalo, atraso, recuperado, situação | `crm_npd/metrics/recompra.py` | `tests/unit/test_recompra.py` |
| Score do vendedor (pesos, carteira) | `crm_npd/metrics/score_vendedor.py` | `tests/unit/test_comissao.py` / performance |
| Funil, conversão, SLA, probabilidade | `crm_npd/metrics/funil.py` + `domain/enums.py` | `tests/unit/test_funil_periodos_percentuais.py` |
| Ciclo 26→25, mês civil, dias úteis | `crm_npd/metrics/periodos.py` | idem |
| % , variação, projeção, ticket | `crm_npd/metrics/percentuais.py` | idem |
| Enums (faixa, etapa, canal) | `crm_npd/domain/enums.py` | — |
| Churn | `crm_npd/repositories/sqlite/churn.py` | `tests/unit/test_repositorios.py` |
| JIT / cancelado / pedidos oficiais | `legacy_monolith.py` (~2462–2535) | golden |
| Identidade GRUPO>REDE>CLIENTE | `legacy_monolith.py` `build_identity_key` (~9715) | — |
| Família de produto | `legacy_monolith.py` `infer_product_family` (~5005) | — |
| Meta empresa/vendedor | `legacy_monolith.py` (~16615) | — |
| Quem é SITE | `legacy_monolith.py` `force_site_vendor_types` (~2373) | — |
| Quem entra no FDB | `crm_npd/repositories/firebird/consultas.py` + `sync/etl.py` | — |

---

## 2. Dois calendários (a causa mais comum de número “não bater”)

Arquivo: `crm_npd/metrics/periodos.py`.
Consumo: dashboard, comissão, mix, metas, operação.

### Ciclo de faturamento (financeiro)

Usado em **faturamento, comissão, meta do vendedor, projeção de meta e mix Novo/Recorrente**.

- Fechamento padrão: **dia 25** (`DIA_FECHAMENTO_PADRAO`).
- Configurável por mês em `ciclos_faturamento` (tela Metas).
- Fórmula: do **dia seguinte ao fechamento do mês anterior** até o **dia de fechamento** do mês de referência.
- Exemplo: setembro/2026 = **26/08 a 25/09**.
- Função: `ciclo_faturamento(referencia, dia_fechamento)`.
- Mês em curso: `ciclo_faturamento_ate_hoje` corta no dia de hoje.

### Mês-calendário (comercial)

Usado em **pedidos** (volume comercial do mês-calendário).

- Dia 1 ao último dia do mês.
- Função: `mes_calendario(referencia)`.

### Dias úteis

- Segunda a sexta, **sem feriado** (`dias_uteis`).
- Introduzir feriado muda toda meta diária histórica.

---

## 3. Identidade comercial

Um “cliente” no CRM **não é uma linha de `customers`**. É uma identidade:

`GRUPO:{id}` > `REDE:{chave}` > `CLIENTE:{id}`

| Peça | Onde |
|---|---|
| Monta a chave | `build_identity_key` em `legacy_monolith.py` ~9715 |
| Tabela | `customer_identity_rules` (identity_key, tipo MATRIZ/FILIAL/CLIENTE) |
| Rede no FDB | `ENTIDADE_001.GRUPO` → `GRUPO_CLI_001` (não existe coluna REDE) |
| Agrupamento por nome | `crm_npd/services/identity.py` (`gerar_identity_key`, stopwords LOJA/FILIAL/LTDA…) |
| Override manual Novo/Recompra | tabela `customer_status_overrides` + `StatusManualCliente` |

Matriz e filiais da mesma rede compartilham histórico e score. O bônus de
cliente novo agora vem do **pedido** no Sisplan (MOTIVO 10), não da primeira
nota da identidade. Override manual continua por identidade.

---

## 4. O que entra (e o que fica de fora) nos totais

### Pedido cancelado

`texto_indica_cancelamento_py` e `sql_pedido_cancelado_cond` (~2448).

Conta como cancelado se `pedido_cancelado = 1` **ou** o texto de situação/motivo
contém: CANCELADO, CANCELADA, CANCELAMENTO, CLIENTE CANCELOU, CANC PELO CLIENTE.

### JIT

Há **três regras diferentes**. Vale ler as três antes de mexer.

**A — Bônus de comissão (nunca para JIT IMEDIATO)**  
`classificar_cliente_novo` em `metrics/comissao.py` e
`compute_vendor_commission`. Pedido com MOTIVO 10 no Sisplan: **todas** as notas
rendem 2%. `05 - JIT IMEDIATO` **entra** no faturamento e na comissão, mas
**nunca** ganha o bônus de novo, nem com override manual.

**B — JIT “picado” (exclui de alguns totais de pedido)**  
`condicao_bloqueia_comissao_jit` (~2466) e `sql_jit_picado_cond` (~2485).

Intenção documentada no comentário:

- **mantém** `98 - JIT` (pedido matriz/base);
- **exclui** `05 - JIT IMEDIATO`;
- o comentário fala em excluir `03 - IMEDIATA` só para CARTUCHOS/SACOLAS
  PERSONALIZADAS, mas o **código Python marca qualquer `03…IMEDIATA`**.
  O SQL `sql_jit_picado_cond` exclui **somente** `05%JIT%IMEDIATO%`.

**C — Pedidos oficiais (Dashboard / “Excel”)**  
`sql_pedidos_realizados_excel_cond` (~2511), validado contra o Excel de mai/2026:

- soma `VALOR_ITEM` por linha (não o total do pedido);
- exige `medida_ref` preenchida;
- exclui cliente com nome `GENERAL LYY`;
- exclui `05 - JIT IMEDIATO`, **exceto** motivo `17 - VAREJO`;
- **inclui** `03 - IMEDIATA` e `98 - JIT`;
- ANFARMA / código `05975`: ignora medida `18X30X10`.

Pedido válido para meta: `sql_pedido_valido_meta_cond` = não cancelado e não JIT picado.

### Canal SITE

`force_site_vendor_types` (~2373) força tipo `Site` em:

- `SITE`
- `AMANDA IZABEL BILYK`
- `ANA BEATRIZ ABDU MENEZES`

Site **não gera comissão**. Entra em faturamento de acompanhamento.

Pedidos e faturamento com responsável `05975` (Associação Nacional / ANFARMA)
entram na carteira da **Joseane (`12667`)**. A origem fica em `vendor_nome_ref`
e aparece no extrato de comissão com o selo ANFARMA.

### Família de produto

`infer_product_family` / `infer_order_product_family` (~5005).

Ordem: Papel Seda → Sacola → Cartucho → Bobina → Envelope → Caixa → Bagkraft.
Um pedido pode ser `Sacola + Cartucho`. Usado em Mercado e mix de produto.

### Faturamento vindo do FDB

`CRM_FATURAMENTO` em `consultas.py`:

- nota sem `MOTIVO_CANCEL`;
- natureza `V` (venda) ou `D` (devolução);
- devolução grava `VALOR_LIQ` **negativo** (soma líquida);
- carteira do vendedor: `PEDIDO_001.RESPONSAVEL` (não o `CODREP` da nota). Sem pedido, cai no representante.

Liquidez: `STATUS = 'DUPL'` e `VAL_DEV` entre 0 e 0,10.

---

## 5. Comissão (dinheiro)

Arquivo da fórmula: `crm_npd/metrics/comissao.py`.  
Orquestração: `compute_vendor_commission` em `legacy_monolith.py` ~10772.  
Tela: extrato do vendedor / Performance.

| Canal | Base | Alíquota | Extra |
|---|---|---|---|
| Interno | soma de `invoices.valor` no **ciclo 26→25** (NF saída + DEMO; devolução não abate) | 1% por linha; 1,75% se bater a meta | Pedido Sisplan MOTIVO 10: todas as notas a 2%. JIT IMEDIATO anula. +1% uma vez sobre o excedente de R$ 100 mil **enquanto não bater a meta** |
| Externo | `liquidez.valor_pago` (senão `valor_liq`) na data de pagamento | 7% | nenhum |
| Site | faturamento só para mostrar | 0 | — |

Constantes (mude aqui):

```
PCT_BASE_INTERNO = 1.0
PCT_BASE_INTERNO_META = 1.75
PCT_NOVO = 2.0
PCT_EXTERNO_LIQUIDEZ = 7.0
LIMITE_BONUS_VOLUME = 100_000.0
PCT_BONUS_VOLUME = 0.01          # 1% do excedente; cai se bater a meta
```

### Cliente novo (Sisplan, MOTIVO 10)

Uma linha é nova **se o pedido vinculado** está marcado no Sisplan como
`10 — PEDIDO PRIMEIRA COMPRA`. **Todas** as notas daquele pedido recebem +1%
(2% no total) e entram nos relatórios como pedido novo.

Exceção: `05 - JIT IMEDIATO` anula o bônus, mesmo se o Sisplan disser que é
cliente novo.

Override em `customer_status_overrides`: `NOVO` força, `RECOMPRA` tira.
JIT IMEDIATO vence o override.

Função pura: `classificar_cliente_novo` + `pedido_sisplan_cliente_novo`.

Bônus de volume: `bonus_volume(total)` = `(total − 100000) × 0,01` se total > 100 mil
**e a vendedora ainda não bateu a meta**. Soma **uma vez** no extrato.

Se a vendedora bate a meta (faturamento do ciclo 26→25 ≥ meta do mês): o bônus
de R$ 100 mil cai; todas as linhas passam a 1,75%; pedidos novos continuam em 2%.
Exemplo: meta de setembro/2026 compara com o faturamento de **26/08 a 25/09**,
o mesmo número da Base Fat da comissão.

Devoluções (`tipo_item_ref = D` ou valor negativo) **não reduz** a base; o
extrato lista o valor para o gestor decidir.

---

## 6. Score do cliente

Arquivo: `crm_npd/metrics/score_cliente.py`.  
Montagem do histórico: `services/performance.py` (`_build_repurchase_map`) e
`services/score.py` (cache).  
Pergunta: “vale a pena ligar para este cliente agora?”.

### Contagem de compras

**Dia único** (aprovado): várias notas no mesmo dia = **uma compra**.
`datas_unicas_ordenadas` em `recompra.py`.

### Três dimensões

```
score_bruto = 0,55×frequência + 0,25×recompra + 0,20×valor
arredonda 1 casa → aplica tetos → limita 0–100 → arredonda 1 casa
```

**Frequência** (`score_frequencia`) — cadência absoluta (mediana de dias):

| Intervalo mediano | Pontos |
|---|---:|
| 1 compra só | 15 |
| sem mediana | 20 |
| ≤ 30 d | 100 |
| ≤ 45 | 95 |
| ≤ 60 | 88 |
| ≤ 90 | 75 |
| ≤ 120 | 60 |
| ≤ 150 | 45 |
| ≤ 180 | 32 |
| ≤ 240 | 20 |
| > 240 | 8 |

**Recompra** (`score_recompra`) — atraso atual ÷ intervalo mediano:

| Razão | Pontos |
|---|---:|
| ≤ 0,80 | 100 |
| ≤ 1,00 | 90 |
| ≤ 1,15 | 75 |
| ≤ 1,35 | 55 |
| ≤ 1,70 | 35 |
| ≤ 2,20 | 18 |
| acima | 5 |

**Valor** (`score_valor`) — faturamento do cliente ÷ maior da carteira **do escopo**:

| Fat / máximo | Pontos |
|---|---:|
| ≥ 0,90 | 100 |
| ≥ 0,70 | 90 |
| ≥ 0,50 | 78 |
| ≥ 0,30 | 62 |
| ≥ 0,15 | 45 |
| ≥ 0,05 | 28 |
| abaixo | 12 |

O mesmo cliente muda de nota se o filtro da tela mudar (a referência muda).

### Tetos

Impedem cliente de compra rara de virar prioridade alta só pelo valor.

| Condição | Teto |
|---|---:|
| 1 compra | 25 |
| mediana > 240 d | 39 |
| mediana > 180 d | 54 |
| mediana > 120 d | 69 |

### Faixas

| Faixa | Piso | Texto |
|---|---:|---|
| A+ | 85 | Elite |
| A | 70 | Muito forte |
| B | 55 | Bom |
| C | 35 | Morno |
| D | 0 | Frio |

Enums e cores: `crm_npd/domain/enums.py` (`FaixaScore`).

### Prioridade da tarefa de recompra

`prioridade_tarefa`:

- A+ ou A **e** atrasado (`atraso ≥ mediana`) → **URGENTE**
- A+ ou A no prazo → **ALTA**
- B → **MÉDIA**
- C/D → **BAIXA**

Cliente fraco atrasado não é urgente.

---

## 7. Recompra e situação

Arquivo: `crm_npd/metrics/recompra.py`.

| Conceito | Fórmula | Função |
|---|---|---|
| Intervalo típico | **mediana** dos dias entre compras (datas únicas) | `intervalo_mediano` |
| Atraso | hoje − última compra | `atraso_atual` |
| Atrasado (lista) | atraso **>** mediana | `esta_atrasado` |
| Fora da janela (score) | atraso **≥** mediana | `esta_fora_da_janela` |
| Situação | ver abaixo | `classificar_situacao` |
| Recuperado | voltou depois de ficar parado **acima** da própria mediana; exige ≥ 2 compras anteriores | `e_cliente_recuperado` |

Situação (`FATOR_EM_RISCO = 1.3`):

| Condição | Classe |
|---|---|
| sem intervalo (ainda não há padrão) | NOVO |
| atraso ≤ intervalo | ATIVO |
| atraso ≤ 1,3 × intervalo | EM RISCO |
| atraso > 1,3 × intervalo | INATIVO |

`intervalo_medio` ainda existe no código (histórico da tela de tarefas) mas a
regra oficial de atraso é a **mediana**.

---

## 8. Score do vendedor

Arquivo: `crm_npd/metrics/score_vendedor.py`.  
Montagem do contexto: `crm_npd/services/performance.py`.  
Comparação **só dentro do canal** (Interno vs Interno, Externo vs Externo).
Um vendedor pode cair de score sem piorar, se a equipe melhorou.

```
score = 0,50×atividade + 0,15×pipeline + 0,10×conversão
      + 0,10×resultado + 0,10×carteira + 0,05×evolução
```

| Dimensão | Como normaliza | Peso |
|---|---|---:|
| Atividade (qtd orçamentos) | linear contra o 1º da equipe | 50% |
| Pipeline (R$ orçamentos) | raiz quadrada contra o 1º | 15% |
| Conversão | taxa × √(n/30) / 60% | 10% |
| Resultado (faturamento) | raiz quadrada contra o 1º | 10% |
| Carteira | linear dos pontos | 10% |
| Evolução vs mês anterior | ver abaixo | 5% |

A raiz evita que um pedido gigante zere os colegas.

**Carteira** (`pontos_carteira`):

- cliente novo = 1,0
- recuperado = 0,8 (já está em “recorrente”; é descontado de lá para não contar 2 vezes)
- recorrente normal = 0,3

**Conversão no score:** 60% é o teto (`CONVERSAO_DE_REFERENCIA`). Abaixo de 30
orçamentos a taxa é amortecida (`AMOSTRA_CONFIAVEL_ORCAMENTOS`), para 1/1 não
valer 100%.

**Evolução:** duas metades de 50 pontos (volume e conversão).

- subiu → 50
- caiu até −10% orçamentos ou −2 p.p. conversão → 25
- caiu mais → 0
- sem base anterior no volume (`None`) → essa metade não pontua
  (`variacao_percentual_ou_nulo`)

Rótulos: ≥85 Destaque, ≥70 Forte, ≥50 Regular, ≥30 Atenção, abaixo Baixa atividade.

Score anual: média simples dos meses (`score_anual`), sem ponderar faturamento.

---

## 9. Funil e conversão

Arquivo: `crm_npd/metrics/funil.py`.  
Etapa **nunca é editada no CRM**: vem do orçamento (OS do FDB).

### Como a etapa é decidida (`determinar_etapa`)

Ordem fixa — não reordene:

1. Tem `pedido_id` não vazio e diferente de `"0"` → **GANHO**
2. Texto `PEDIDO APROVADO` / `PEDIDO FATURADO` → **GANHO**
3. Texto CANCELADO / PERDIDO / SEM PERFIL → **PERDIDO**
4. Código de status `F` sem desfecho → **ENCERRADO**
5. Textos de APROVAÇÃO / NEGOCIAÇÃO / CONTATO
6. Default **NOVO**

Listas de texto: `TEXTOS_GANHO`, `TEXTOS_PERDIDO`, `TEXTOS_APROVACAO`,
`TEXTOS_NEGOCIACAO`, `TEXTOS_CONTATO` no mesmo arquivo. Comparação sem acento.

### Probabilidade (pipeline ponderado)

Em `EtapaFunil.probabilidade` (`domain/enums.py`):

| Etapa | % | Sai do pipeline? | SLA (parado)? |
|---|---:|---|---|
| NOVO | 15 | não | não |
| CONTATO | 30 | não | sim (14 dias) |
| NEGOCIACAO | 60 | não | sim |
| APROVACAO | 80 | não | sim |
| GANHO | 100 | sim | não |
| PERDIDO | 0 | sim | não |
| ENCERRADO | 0 | sim | não |

Valor ponderado = `valor × probabilidade / 100`.  
SLA: `SLA_DIAS_PARADO = 14` só em etapa acionável.

### Duas “conversões” (nomes diferentes de propósito)

**Conversão de proposta** (Estudo de Mercado, Performance, `get_metrics_orcamentos`):

```
pedido_id preenchido  OU  desc_status = PEDIDO APROVADO
```

Função: `orcamento_convertido`.  
No repositório: `crm_npd/repositories/sqlite/orcamentos.py` → `metricas`.
Denominador = todos os orçamentos do período (não só ganhos+perdidos).

**Taxa do funil** (`taxa_conversao` em `funil.py`):

```
ganhos / (ganhos + perdidos)
```

ENCERRADO não entra no denominador.

**Taxa de faturamento** (Operação, antigo rótulo “CONVERSÃO QTD”):

```
notas / pedidos × 100
```

Função: `taxa_faturamento_pedidos`. **Não compare** com a conversão de proposta.

Atenção ao revisar: `determinar_etapa` ignora `pedido_id = "0"`, mas
`orcamento_convertido` aceita qualquer texto não vazio (incluindo `"0"`).

---

## 10. Churn

Arquivo: `crm_npd/repositories/sqlite/churn.py`.  
Tela: Estudo de Mercado.

Identidade que faturou no **ano anterior** e **não faturou** de 1º de janeiro
do ano atual até a data de referência.

Agrupa por `identity_key` (fallback `CLIENTE:{id}`). Não usa `rede_key`.

---

## 11. Metas, projeção e tickets

### Meta da empresa

`meta_empresa` (~16626):

1. se existir setting `meta_mensal` (tela Metas), usa esse valor;
2. senão, média dos **3 últimos ciclos de pedido fechados** (`meta_empresa_calc`).

### Meta do vendedor

`meta_vendedor` (~16654):

1. `metas_vendedores.meta_manual` se > 0;
2. senão, média dos 3 últimos **ciclos de faturamento** fechados daquele vendedor.

A meta do vendedor é de **faturamento do ciclo 26→25** (o mesmo número da
comissão). Setembro/2026 = 26/08 a 25/09.

### Painel de meta

`painel_meta` em `percentuais.py`:

- % = faturado / meta
- falta = max(meta − faturado, 0)
- por dia = falta / dias úteis restantes

### Projeção

`projecao_linear` = `realizado / dias_decorridos × dias_totais`.
Ritmo constante; no começo do mês costuma subestimar.

### Variação %

- Telas comuns: `variacao_percentual` — base zero e atual > 0 vira **+100%**.
- Score do vendedor: `variacao_percentual_ou_nulo` — sem base anterior devolve
  `None` (não pontua evolução no máximo).

### Ticket

- Pedidos: soma de `valor_item` das linhas válidas ÷ quantidade de **pedidos únicos**.
- Faturamento/comissão: valor das notas ÷ quantidade de notas.
- Função genérica: `percentuais.ticket_medio`.

---

## 12. Mix Novo / Recorrente / Recuperado

Ainda **não unificado** (decisão 2 da reconciliação).

| Tela | Unidade | Onde |
|---|---|---|
| Comissão / Performance | identidade; NOVO = só a 1ª nota | `compute_vendor_commission`, `services/performance.py` |
| Algumas listas de time | pedido único | `summarize_team_status_by_orders` no monolito |

Recuperado é **subconjunto** de recorrente: voltou depois de atraso acima da mediana.

---

## 13. Percentuais e divisão por zero

`crm_npd/metrics/percentuais.py`

- `percentual(parte, total)` → 0 se total = 0
- `variacao_percentual` → 100% se só o atual existe
- `variacao_percentual_ou_nulo` → None se só o atual existe (score)

Não unifique essas duas variações sem revisar o score do vendedor.

---

## 14. O que o FDB manda para as fórmulas

`crm_npd/sync/etl.py` + `consultas.py`. Não é fórmula, mas muda o número se o
filtro mudar.

| Fato CRM | Origem FDB | Campo de valor |
|---|---|---|
| `orders` | `PEDIDO_001` + `PED_ITEN_001` | `PRECO × QTDE` em `valor` e `valor_item_ref` |
| `invoices` | `NOTA_001` + `NOTAITEN_001` | `VALOR_LIQ` (D negativo) |
| `orcamentos` | `OS_001` + soma de `OS_ITEN_001` | `VALOR_TOTAL` / `VALOR_COB` |
| `liquidez` | `RECEBER_001` `STATUS=DUPL` | `VALOR2` e `VALOR_PAGO` |

Data sentinela Sisplan `1899-12-30` vira vazia no ETL.

---

## 15. Mapa rápido tela → cálculo

| Tela | O que mostra | Funções |
|---|---|---|
| Dashboard | ciclo vs mês, meta, tickets, canais | `periodos`, `percentuais`, SQL de pedidos oficiais, `invoices` |
| Extrato / comissão | R$ a pagar | `compute_vendor_commission` → `metrics/comissao` |
| Performance | score do vendedor | `services/performance.py` → `score_vendedor` |
| Clientes / score | faixa A+…D | `score_cliente` + `recompra` |
| Tarefas | urgência de recompra | `prioridade_tarefa` |
| Funil / Oportunidades | etapa e parado | `determinar_etapa`, `esta_parado` |
| Orçamentos detalhe | fases e % | status da OS + `get_metrics_orcamentos` |
| Estudo de Mercado | conversão, churn, família | `orcamento_convertido`, `churn.py`, `infer_product_family` |
| Metas | meta manual ou média 3 ciclos | `meta_empresa` / `meta_vendedor` |
| Operação | taxa de faturamento | `taxa_faturamento_pedidos` |

---

## 16. Divergências que ainda existem (para a sua revisão)

1. **JIT 03-IMEDIATA:** o comentário em `condicao_bloqueia_comissao_jit` restringe
   por grupo; o `if` não olha o grupo. O SQL oficial de pedidos **não** exclui 03.
2. **Atrasado:** lista usa `>` (`esta_atrasado`); score/tarefa usa `≥`
   (`esta_fora_da_janela`).
3. **pedido_id = "0":** conversão de proposta conta; etapa do funil não vira GANHO.
4. **Mix Novo:** identidade numa tela, pedido em outra (decisão 2 em aberto).
5. **Status da OS:** o FDB manda código (`09`, `19`, `40`). Só textos como
   `PEDIDO APROVADO` entram nas listas do funil. Códigos sem texto caem em NOVO
   ou no próprio código na tela de orçamentos.

Quando for alterar um número de comissão ou de faixa, descreva no commit **qual
regra mudou e por quê**. O golden master trava a base antiga: só atualize o
baseline se a mudança for aprovada.
