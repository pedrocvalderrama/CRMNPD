# Mapeamento do 14215F.FDB

Cópia local do Sisplan (`14215F.FDB`, Firebird 3 Embedded). **Somente SELECT.**
Não há PK/FK declaradas no catálogo: os joins abaixo são lógicos, pelo nome da coluna.

| Arquivo | O que contém |
|---|---|
| `docs/esquema-firebird/catalogo.json` | 1926 tabelas + 1 view, todas as colunas e tipos (~4,6 MB) |
| `docs/esquema-firebird/tabelas.tsv` | índice `tabela / qtd colunas / pk / fk` |
| `docs/esquema-firebird/indice-por-prefixo.md` | as 1926 tabelas agrupadas por prefixo |
| `docs/esquema-firebird/colunas-crm.txt` | dump das colunas das tabelas do CRM |
| `docs/esquema-firebird/csv-para-fdb.tsv` | campo do CSV → coluna Firebird |

View única: `V_ACESSO_MENU`. Prefixo `_001` = empresa/filial padrão desta base.

Contagens lidas nesta cópia:

| Tabela | Linhas | Papel no CRM |
|---|---:|---|
| `ENTIDADE_001` | 12.610 | cliente / fornecedor / transportadora |
| `REPRESEN_001` | 62 | representante / vendedor |
| `PRODUTO_001` | 6.038 | SKU |
| `PEDIDO_001` | 21.311 | cabeçalho do relatório 721 |
| `PED_ITEN_001` | 37.926 | item do 721 |
| `NOTA_001` | 35.188 | cabeçalho do relatório 731 |
| `NOTAITEN_001` | 67.313 | item do 731 |
| `RECEBER_001` | 52.205 | relatório 733 |
| `OS_001` | 9.914 | cabeçalho do relatório 104 |
| `OS_ITEN_001` | 17.048 | item do 104 |
| `ORCAMENTO_001` | **0** | módulo antigo, vazio nesta base |
| `ORCA_ITEN_001` | **0** | vazio |
| `NATUREZA_001` | — | tipo fiscal V/D/… |
| `GRUPO_CLI_001` | 10 | grupo comercial (rede) |
| `GRUPO_PA_001` | — | família do produto |
| `CAD_PROSP_001` | 470 | prospecção |
| `PESSOAL_001` | 167 | funcionário |
| `CADCEP_001` | — | cidade / UF pelo CEP |
| `EMPRESA` | — | empresa emitente |

```
ENTIDADE_001.CODCLI ──┬── PEDIDO_001.CODCLI ──── PED_ITEN_001.NUMERO
                      ├── NOTA_001.CODCLI ────── NOTAITEN_001.FATURA
                      ├── RECEBER_001.CODCLI
                      └── OS_001.CLIENTE ─────── OS_ITEN_001.NUMERO

REPRESEN_001.CODREP ──┬── PEDIDO_001.CODREP
                      ├── NOTA_001.CODREP
                      ├── RECEBER_001.CODREP
                      └── OS_001.CODREP / COD_VEN

PRODUTO_001.CODIGO ───┬── PED_ITEN_001.CODIGO
                      ├── NOTAITEN_001.CODIGO
                      └── OS_ITEN_001.CODIGO

NATUREZA_001.NATUREZA ── NOTA_001 / NOTAITEN_001 / PEDIDO_001
GRUPO_CLI_001.CODIGO ─── ENTIDADE_001.GRUPO
GRUPO_PA_001.CODIGO ──── PRODUTO_001.GRUPO
CADCEP_001.CEP ───────── ENTIDADE_001.CEP
```

Os SELECTs canônicos já existentes estão em `crm_npd/repositories/firebird/consultas.py`
(`FATURAMENTO_LIQUIDO`, `PEDIDOS`, `LIQUIDEZ`). Falta o de orçamento (`OS_*`).

---

## Identidade comercial (substitui o CSV)

No CRM a identidade é `GRUPO:…` > `REDE:…` / `MATRIZ:…` > `CLIENTE:…`.
No Firebird isso se monta assim:

| Nível CRM | Origem Firebird |
|---|---|
| CLIENTE | `ENTIDADE_001.CODCLI` + `NOME` / `FANTASIA` |
| GRUPO / REDE | `ENTIDADE_001.GRUPO` → `GRUPO_CLI_001.CODIGO` / `DESCRICAO` (CASAS AJITA, SUSHIAKI, VOLT…) |
| MATRIZ / filial | não existe coluna `REDE`. Cruzar `CNPJ` (raiz) e `TIPO_ENTIDADE` |
| Cidade / UF | `ENTIDADE_001.CEP` → `CADCEP_001.NOMCEP` / `ESTCEP` (o 104 chama a cidade de `NOME_1`) |
| Representante | `REPRESEN_001.CODREP` + `NOME` / `FANTASIA` |
| Tipo de entidade | `TIPO` + `TIPO_ENTIDADE` (`C` cliente, `F` fornecedor, `T` transportadora, combinações `CF`, `CT`…) |

`ORCAMENTO_001` **não serve**. O relatório 104 do Sisplan é a **Ordem de Serviço**.

---

## 721 Pedidos → `PEDIDO_001` + `PED_ITEN_001`

Uma linha do CSV 721 = um item (`PED_ITEN_001`) com o cabeçalho (`PEDIDO_001`).

`PEDIDO_001.SITUACAO` está vazio nesta base. O status real é `PEDIDO_001.STATUS` (código):
`11` (7.636), `15` (5.412), `19` (5.034), `14` (1.507), `10` (1.457), `09` (118).
`STATUSPED` é histórico de mudança, não a tabela de rótulos.

| CSV 721 | Firebird | Notas |
|---|---|---|
| `pedido` | `PEDIDO_001.NUMERO` = `PED_ITEN_001.NUMERO` | VARCHAR(16) |
| `emissao_pedido` / `data` | `PEDIDO_001.DT_EMISSAO` | |
| `entrega_pedido` | `PEDIDO_001.ENTREGA` | |
| `fantasia` | `ENTIDADE_001.FANTASIA` | join `CODCLI` |
| `cliente` | `ENTIDADE_001.CODCLI` + `NOME` | |
| `representate` / `representante` | `REPRESEN_001.NOME` | `PEDIDO_001.CODREP` |
| `responsavel` / `nome_responsavel` | `PEDIDO_001.RESPONSAVEL` → `REPRESEN_001` ou `PESSOAL_001` | |
| `valor_ped` | `PEDIDO_001.VLR_LIQ` | total do pedido |
| `valor_item` | `PED_ITEN_001.PRECO * QTDE` (ou `PRECO_VENDA`) | o CSV já traz o total do item |
| `preco` | `PED_ITEN_001.PRECO` | |
| `qtde_total` / `qtde` | `PED_ITEN_001.QTDE` | `QTDE_F` faturada, `QTDE_CANC` cancelada |
| `codigo` | `PED_ITEN_001.CODIGO` = `PRODUTO_001.CODIGO` | |
| `descricao` | `PRODUTO_001.DESCRICAO` | |
| `grupo` | `PRODUTO_001.GRUPO` → `GRUPO_PA_001.DESCRICAO` | família do produto, não o grupo do cliente |
| `medida` | `PED_ITEN_001.TAM` | |
| `papel` | `PRODUTO_001` / etiqueta `ETQ_PROD_001` | confirmar no item se o rótulo do CSV for texto livre |
| `tipo_item_pedido` | `PED_ITEN_001.TIPO` | |
| `condicao_entrega` | `PEDIDO_001.TIPO_ENTREGA` / `COND_PAGTO` | |
| `situacao` | `PEDIDO_001.STATUS` | código; o CSV traz o rótulo do relatório 721 |
| `motivo` | `PEDIDO_001.MOTIVO` | `TIPO_MOTIVO_001.DESCRICAO` |
| `obs` | `PEDIDO_001.OBS` | BLOB |
| `cidade` / `uf` | `CADCEP_001` via `ENTIDADE_001.CEP` | |
| `ddd` / `telefone` / `email` | `ENTIDADE_001` / `CLI_DDD` / `CLI_FONE` no pedido | |

Filtro sugerido do SELECT já existente: `PEDIDO_001.DT_EMISSAO BETWEEN ? AND ?`.

---

## 731 Faturamento → `NOTA_001` + `NOTAITEN_001`

Uma linha do CSV 731 = um item de nota.

| CSV 731 | Firebird | Notas |
|---|---|---|
| `nota` / `nf` | `NOTA_001.FATURA` = `NOTAITEN_001.FATURA` | |
| `pedido` | `NOTAITEN_001.PEDIDO` | |
| `dt_emissao` | `NOTA_001.DT_EMISSAO` | ciclo 26→25 usa esta data |
| `fantasia` | `ENTIDADE_001.FANTASIA` | `NOTA_001.CODCLI` |
| `cliente` | `ENTIDADE_001.CODCLI` + `NOME` | `CLI_RAZAO` também está na nota |
| `codresp` / `nome_responsavel` | `PEDIDO_001.RESPONSAVEL` via `NOTAITEN_001.PEDIDO` | fallback `NOTA_001.CODREP` se a NF não tiver pedido |
| `representante` | `REPRESEN_001.NOME` | |
| `produto` | `PRODUTO_001.DESCRICAO` | `NOTAITEN_001.CODIGO` |
| `qtde` | `NOTAITEN_001.QTDE` | |
| `preco` | `NOTAITEN_001.PRECO` | |
| `valor` | `NOTAITEN_001.VALOR_LIQ` | valor líquido do item |
| (frete do item) | `NOTAITEN_001.VALOR_FRETE` | |
| (cancelamento) | `NOTA_001.MOTIVO_CANCEL` | vazio = nota válida |
| (tipo fiscal) | `NATUREZA_001.TIPO` | `V` venda (53 naturezas), `D` devolução (17) |

SELECT canônico (`FATURAMENTO_LIQUIDO`):

- `NAT.TIPO IN ('V', 'D')`
- `MOTIVO_CANCEL` nulo ou em branco
- `DT_EMISSAO` no ciclo

A diferença já medida (FDB 647 linhas / R$ 1.104.404,26 líquido vs CSV/SQLite 518 / R$ 997.044,89)
é filtro extra do relatório 731 (empresa, comissionável, JIT, data), não erro de tabela.

---

## 104 Orçamento → `OS_001` + `OS_ITEN_001`

`ORCAMENTO_001` / `ORCA_ITEN_001` existem no schema e estão **vazias**.
O 104 (`status_os`, `prospeccao`, `dt_prev`, `dt_retorno`, `tipo_contato`, `valort`) casa com a OS.

| CSV 104 | Firebird | Notas |
|---|---|---|
| `numero` | `OS_001.NUMERO` | VARCHAR(12) |
| `cliente` | `OS_001.CLIENTE` → `ENTIDADE_001.CODCLI` | não é `CODCLI` |
| `nome` | `ENTIDADE_001.NOME` / `FANTASIA` | |
| `pedido` | `OS_001.PEDIDO` | preenchido quando virou pedido |
| `cod_ven` / `nomeven` | `OS_001.COD_VEN` → `REPRESEN_001` | |
| `represen` / `nome_rep` | `OS_001.CODREP` → `REPRESEN_001` | |
| `dt_inicio` | `OS_001.DT_INICIO` | |
| `dt_prev` / `data_prev` | `OS_001.DT_PREV` | sentinela Sisplan `1899-12-30` = sem data |
| `dt_retorno` | `OS_001.DT_RETORNO` | idem sentinela |
| `dt_validade` | `OS_001.DT_VALIDADE` | |
| `status` | `OS_001.STATUS` | `A` aberto, `F` fechado (amostra) |
| `status_os` | `OS_001.STATUS_OS` | código (`19`, `40`, `09`…) |
| `desc_status` | cadastro PED (`crm_npd/domain/status_os.py`) + `TIPO_MOTIVO_001` | ex. `40 - MANUTENCAO DE CARTEIRA` |
| `prospeccao` | `OS_001.PROSPECCAO` | |
| `desc_prospeccao` | `CAD_PROSP_001.NOME` / `FANTASIA` | join pelo código |
| `tipo_contato` | `OS_001.TIPO_CONTATO` | `LIGACAO`, `WHATSAPP`, `E-MAIL`, `VISITA`, `OUTROS` |
| `motivo` / `descmotivo` | `OS_001.MOTIVO` → `TIPO_MOTIVO_001` | |
| `obs` / `observacao` | `OS_001.OBS` / `OBSERVACAO` | |
| `quantidade` | `OS_001.QTDE` ou soma `OS_ITEN_001.QTDE` | cabeçalho recente veio nulo; o item tem quantidade |
| `valor` (base) | `OS_001.VALOR` ou soma itens `PRECO * QTDE` | |
| `valort` (total) | `OS_001.VALOR_COB` ou soma `OS_ITEN_001.VALOR_TOTAL` | no CSV, `VALORT` é o total final |
| `funcionario` | `OS_001.FUNCIONARIO` | |
| `nome_funcionario` | `PESSOAL_001.NOME` | `PESSOAL_001.CODIGO` |
| `cep` | `OS_001.CLI_CEP` ou `ENTIDADE_001.CEP` | |
| `cod_uf` / `nome_1` | `CADCEP_001.ESTCEP` / `NOMCEP` | |

Amostra recente: OS `011101` cliente `02636`, `STATUS=F`, `STATUS_OS=19`, contato `LIGACAO`, pedido `23084`.

---

## 733 Liquidez → `RECEBER_001`

Uma linha do CSV 733 = um título (duplicata / parcela).

| CSV 733 | Firebird | Notas |
|---|---|---|
| `duplicata` / `titulo` | `RECEBER_001.NUMERO` | |
| `parcela` / `qtdeparcelas` | `RECEBER_001.PARCELA` / `QTDEPARCELAS` | |
| `fatura` | `RECEBER_001.FATURA` | liga na `NOTA_001` |
| `pedido` | `RECEBER_001.PEDIDO` | |
| `dt_emissao` | `RECEBER_001.DT_EMISSAO` | |
| `dt_pagto` | `RECEBER_001.DT_PAGTO_DUPLI` (ou `DT_STATUS` se pago) | |
| `dt_vencimento` | `RECEBER_001.DT_VENCTO` | |
| `valor_liq` | `RECEBER_001.VALOR2` | SELECT canônico usa `VALOR2` |
| `valor_pago` | `RECEBER_001.VALOR_PAGO` | |
| `val_nota` | `RECEBER_001.VALOR` | |
| `desconto` | `RECEBER_001.DESCONTO` | |
| `val_frete` | `RECEBER_001.FRETE` | |
| `fantasia` / `cliente` | `ENTIDADE_001` via `CODCLI` | |
| `codrepre` / `nome_representante` | `RECEBER_001.CODREP` | |
| `codresp` / `nome_responsavel` | `CODREP` / `CODREP2` | |
| `empresa` | `EMPRESA` / `RECEBER_001.EMP_ID` | |
| (filtro oficial) | `STATUS = 'DUPL'` | 49.080 títulos `DUPL`/`01` |
| (filtro oficial) | `VAL_DEV` entre 0 e 0,10 | descarta devolução relevante |

Outros `STATUS` de `RECEBER_001` (não entram no SELECT canônico): `AGRU`, `ANTE`, `CANC`, `DESD`.
`TABSIT_001` descreve a situação de cobrança (`01` COBRANÇA SIMPLES, `09` LIQUIDADA, `21` PIX/TED/DOC…).

---

## Tabelas satélite que o CRM vai precisar

| Tabela | Uso |
|---|---|
| `NATUREZA_001` | `TIPO`: `V` venda, `D` devolução, `B`/`I`/`O`/`P`/`R`/`S` outros |
| `GRUPO_CLI_001` | redes comerciais (`CODIGO`, `DESCRICAO`) |
| `GRUPO_PA_001` | família do produto (`grupo` do 721) |
| `MARCA_001` / `ETQ_PROD_001` | marca e etiqueta |
| `CAD_PROSP_001` | nome da prospecção do 104 |
| `PESSOAL_001` | nome do funcionário da OS |
| `TIPO_MOTIVO_001` | descrição do motivo (5 registros) |
| `CADCEP_001` / `CIDADE` / `TABUF_001` | cidade e UF |
| `TABSIT_001` | situação do título |
| `SITCLI_001` | situação do cliente (`ENTIDADE_001.SIT_CLI`) |
| `SITNOTA_001` | situação da nota |
| `EMPRESA` | emitente / filtro multiempresa |
| `HIST_MOTIVO` / `HIST_PEDIDO_001` | auditoria, não fato comercial |

Prefixos úteis no índice completo: `PED` (18), `NOTA` (23), `OS` (23), `ORCA` (9, vazio), `CAD` (34), `ENTID`/`ENTIDADE`, `GRUPO`, `CLI`, `TAB`, `RECEBER`.

---

## Como achar qualquer tabela

1. Abrir `docs/esquema-firebird/indice-por-prefixo.md` e localizar o prefixo (`PED`, `OS`, `CAD`…).
2. Abrir `catalogo.json` → `detalhe.<TABELA>.colunas` para tipo e posição.
3. Confirmar volume com `SELECT COUNT(*) FROM <TABELA>` (já validado no Embedded).

Regenerar o catálogo (somente leitura):

```
.\.venv\Scripts\python.exe scripts\mapear_firebird.py
.\.venv\Scripts\python.exe scripts\_gerar_indice_esquema.py
```

---

## O que este mapa destrava — e o que ainda não mudou

O CRM lê somente o FDB. `CRM_NPD_FONTE=firebird`. A tela Atualizar Sisplan
dispara `sincronizar_crm` (SELECT no Firebird → tabelas de fato no SQLite).
Na fábrica a conexão é `192.168.100.168:3051` → `C:\Sisplan\Base\14215F.FDB`.
