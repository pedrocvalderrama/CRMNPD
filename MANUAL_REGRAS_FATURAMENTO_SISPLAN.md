# Manual de Regras de Negócio e Extração de Faturamento - ERP Sisplan

Este documento descreve todas as especificações técnicas, regras contábeis/fiscais de negócio, modelagem de dados e consultas SQL utilizadas para apurar o **Faturamento Líquido** do grupo empresarial (**NPD Embalagens, Compre Sacola, CS Comércio de Embalagens e DEMO**) a partir do **ERP Sisplan (Firebird)** e fontes integradas.

---

> [!CAUTION]
> ### 🚨 REGRA CRÍTICA DE SEGURANÇA: LEITURA ESTRITA (READ-ONLY)
> - O banco do Sisplan (`14215F.FDB`) é o banco **em produção ativo** da empresa.
> - **Nunca execute `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER` ou `CREATE`.**
> - Todas as transações com o Firebird devem ser abertas estritamente em modo de leitura (`READ ONLY`).

---

## 1. Parâmetros de Conexão com o Banco de Dados

| Parâmetro | Valor | Observação |
|---|---|---|
| **Host / IP** | `192.168.100.168` (ou hostname `servidorge`) | Servidor local do Sisplan. |
| **Porta** | **`3051`** | **Importante:** Firebird 3 roda na porta `3051`. |
| **Banco de Dados** | `C:\Sisplan\Base\14215F.FDB` | Caminho físico no servidor (ou `V:\Base\14215F.FDB` via rede). |
| **Usuário** | `SYSDBA` | Usuário administrativo. |
| **Senha** | `masterkey` | Senha padrão. |
| **Driver / DLL (64 bits)** | `fbclient_x64.dll` | Biblioteca 64-bit para conexões Python (`fdb.load_api`). |
| **Dialeto Firebird** | Dialeto 3 | Suporta SQL ANSI padrão. |

---

## 2. Visão Geral da Arquitetura do Faturamento Líquido

O faturamento líquido da empresa **não reside em uma única tabela**. Ele é composto pelo consolidado de **4 fontes de dados**:

```
                         FATURAMENTO LÍQUIDO REAL
                                    │
    ┌───────────────────────────────┼───────────────────────────────┐
    ▼                               ▼                               ▼
[FONTE A: NFs de Saída]     [FONTE B: NFs de Entrada]       [FONTE C: DEMO Financeiro]
 • Vendas Brutas (TIPO='V')  • Devoluções de Clientes       • Duplicatas a Receber
 • Sem entrega futura        • TIPO='D' ou CFOPs 1.201/     • Tabela: RECEBER_001
 • Sem frete / sem clichê      2.201 ou Descrição Dev       • EMP_ID = 3
 • (-) Devoluções Saída      • Deduz do faturamento         • STATUS = 'DUPL'
```

$$\text{Faturamento Líquido} = (\text{Vendas NFs} + \text{DEMO Financeiro}) - \text{Devoluções (Saída + Entrada)}$$

---

## 3. Fontes de Dados e Regras de Extração

### 3.1. Fonte A: Notas Fiscais de Saída (Tabelas `NOTA_001` e `NOTAITEN_001`)

- **Objetivo**: Extrair todas as notas de venda emitidas e eventuais notas de devolução de saída.
- **Tabelas Envolvidas**:
  - `NOTA_001` ($N$): Cabeçalho da nota fiscal (`FATURA`, `DT_EMISSAO`, `CODCLI`, `CODREP`, `EMP_ID`, `MOTIVO_CANCEL`).
  - `NOTAITEN_001` ($NI$): Itens da nota (`FATURA`, `CODIGO`, `QTDE`, `VALOR_LIQ`, `VALOR_FRETE`, `NATUREZA`).
  - `NATUREZA_001` ($NAT$): Natureza de operação / CFOP (`NATUREZA`, `TIPO`).
  - `PRODUTO_001` ($PROD$): Cadastro de produtos (`CODIGO`, `DESCRICAO`).
  - `ENTIDADE_001` ($E$): Cliente (`CODCLI`, `NOME`, `FANTASIA`).
  - `REPRESEN_001` ($REP$): Representante comercial (`CODREP`, `NOME`).
  - `EMPRESA` ($EMP$): Unidade faturadora (`EMP_ID`, `EMP_NOME`, `EMP_FANTASIA`).
- **Regras de Negócio e Filtros**:
  1. **Filtro de Cancelamento**: `(N.MOTIVO_CANCEL IS NULL OR TRIM(N.MOTIVO_CANCEL) = '')`. Notas canceladas são sumariamente ignoradas.
  2. **Classificação de Natureza Fiscal**:
     - `NAT.TIPO = 'V'`: Vendas normais.
     - `NAT.TIPO = 'D'`: Devoluções de venda.
     - 🚫 **Desconsiderar CFOPs de entrega futura** (`5.116`, `6.116`, `5.117`, `6.117`, `6.1162`): no Sisplan vêm como `TIPO = 'R'` (remessa) e **não entram** no faturamento.
  3. **Frete de Saída**: O frete faturado na nota (`NI.VALOR_FRETE`) NÃO DEVE somar ao faturamento bruto de vendas e ao representante.
  4. **Custo de Cliche**: Desconsiderar os valores de cliches, desconsiderar os valores das linhas onde o DESCGRUPO tenha valor de "CLICHES".
  5. **Atribuição ao vendedor**: o faturamento entra na carteira do **responsável do pedido** (`PEDIDO_001.RESPONSAVEL` via `NOTAITEN_001.PEDIDO`), não do representante da nota (`NOTA_001.CODREP`). Se a linha não tiver pedido, usa o representante da nota.

### 3.2. Fonte B: Notas Fiscais de Entrada por Devolução (Tabelas `NOTA_ENTRA_001` e `NF_ENTRA_ITEN_001`)

- **Objetivo**: Capturar devoluções emitidas pelos clientes que dão entrada no almoxarifado/fábrica.
- **Tabelas Envolvidas**:
  - `NOTA_ENTRA_001` ($NE$): Cabeçalho da nota de entrada (`NOTAFISCAL`, `SERIE`, `CREDOR`, `DT_EMISSAO`, `EMP_ID`, `CODSIT_NOTA`).
  - `NF_ENTRA_ITEN_001` ($NEI$): Itens de entrada (`NUMERO`, `SERIE`, `CODCRE`, `CODIGO`, `QTDE_EST`, `TOTAL`, `VAL_FRETE`, `NATUREZA`).
  - `ENTIDADE_001` ($E$): Cliente/Credor remetente da devolução (`CODCLI = NE.CREDOR`).
  - `REPRESEN_001` ($REP$): Vincula ao representante da carteira do cliente (`E.CODREP = REP.CODREP`).
- **Regras de Negócio e Filtros**:
  1. **Filtro de Situação**: `(NE.CODSIT_NOTA IS NULL OR NE.CODSIT_NOTA != '02')`. Situação `02` indica nota cancelada/rejeitada.
  2. **Critério de Identificação de Devolução**:
     ```sql
     AND (
         NAT.TIPO = 'D' 
         OR NAT.DESCRICAO LIKE '%DEVOLU%PRODUCAO%' 
         OR NAT.DESCRICAO LIKE '%DEVOLU%VENDA%' 
         OR NAT.NATUREZA IN ('1.201', '2.201')
     )
     ```
  3. **Dedutibilidade**: Toda linha de entrada aprovada entra com `NAT_TIPO = 'D'`. O valor (`TOTAL + VAL_FRETE`) e a quantidade (`QTDE_EST`) são subtraídos do faturamento líquido, do representante e da empresa.


### 3.4. Fonte C: Faturamento Empresa DEMO (Tabela `RECEBER_001`)

- **Objetivo**: A unidade DEMO opera financeiramente via títulos a receber no Sisplan.
- **Tabela**: `RECEBER_001` ($R$).
- **Regras de Negócio e Filtros**:
  ```sql
  SELECT 
      R.DT_EMISSAO,
      CAST(COALESCE(R.VALOR2, 0) AS DOUBLE PRECISION) AS VALOR_PARCELA
  FROM RECEBER_001 R
  WHERE R.DT_EMISSAO BETWEEN ? AND ?
    AND R.EMP_ID = 3
    AND COALESCE(R.VAL_DEV, 0) BETWEEN 0.00 AND 0.10
    AND R.STATUS = 'DUPL'
  ```
  - `EMP_ID = 3`: Identificador exclusivo da empresa DEMO.
  - `STATUS = 'DUPL'`: Títulos do tipo duplicata válida.
  - `COALESCE(VAL_DEV, 0) BETWEEN 0.00 AND 0.10`: Desconsidera devoluções de duplicatas.
  - Coluna de valor: Sempre utilizar `VALOR2` (valor nominal da parcela).
  - **Alocação**: Soma diretamente na empresa `DEMO` e no faturamento geral do dia e do mês.

