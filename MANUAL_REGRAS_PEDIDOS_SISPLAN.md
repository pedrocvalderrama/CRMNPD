# Manual de Regras de Negócio e Extração de Pedidos - ERP Sisplan

Este manual descreve todas as especificações técnicas, regras de negócio, modelagem de dados e queries SQL utilizadas para ler e processar os **Pedidos de Vendas** do **ERP Sisplan (Firebird)**. Ele serve como especificação para o desenvolvimento de novos sistemas, automações e integrações.

---

> [!CAUTION]
> ### 🚨 REGRA CRÍTICA DE SEGURANÇA: LEITURA ESTRITA (READ-ONLY)
> - O banco do Sisplan (`14215F.FDB`) é o banco **em produção ativo** da fábrica e da expedição.
> - **Nunca execute `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER` ou `CREATE`.**
> - Todas as transações devem ser abertas estritamente em modo de leitura (`READ ONLY`).

---

## 1. Parâmetros de Conexão com o Banco de Dados

| Parâmetro | Valor | Observação |
|---|---|---|
| **Host / IP** | `192.168.100.168` (ou hostname `servidorge`) | IP local do servidor do Sisplan. |
| **Porta** | `3051` | Firebird 3 roda na porta **3051** (e não na 3050). |
| **Banco de Dados** | `C:\Sisplan\Base\14215F.FDB` | Caminho local no servidor (ou `V:\Base\14215F.FDB` via mapeamento). |
| **Usuário** | `SYSDBA` | Usuário padrão administrativo. |
| **Senha** | `masterkey` | Senha padrão do Firebird. |
| **Driver / DLL (64 bits)** | `fbclient_x64.dll` | Em aplicações Python 64-bit, obrigatório apontar para a DLL de 64 bits. |
| **Dialeto Firebird** | Dialeto 3 | Suporta SQL ANSI padrão, subqueries e funções analíticas básicas. |

---

## 2. Modelagem das Tabelas de Pedidos

A extração de pedidos exige a junção de duas tabelas centrais (`PEDIDO_001` e `PED_ITEN_001`) com várias tabelas de apoio cadastral.

```
       PEDIDO_001 (Cabeçalho do Pedido)
       ├── NUMERO (PK)
       ├── DT_EMISSAO
       ├── CODCLI ────────────────────────► ENTIDADE_001 (Cliente)
       │                                     └── CEP ────────► CADCEP_001 ──► CIDADE (UF, Cidade)
       ├── CODREP ────────────────────────► REPRESEN_001 (Vendedor / Representante)
       ├── RESPONSAVEL ───────────────────► ENTIDADE_001 (Vendedor interno / Carteira)
       ├── PERIODO ───────────────────────► TABPRZ_001 (Prazo / Condição de Entrega)
       ├── MOTIVO ────────────────────────► MENSAGEM_001 (Motivo do Pedido)
       └── STATUS ────────────────────────► SITPROD_001 (Situação de Produção)

       PED_ITEN_001 (Itens do Pedido)
       ├── NUMERO (FK -> PEDIDO_001.NUMERO)
       ├── CODIGO ────────────────────────► PRODUTO_001 (Produto)
       │                                     ├── GRUPO ──────► GRUPO_PA_001 (Categoria de Produto)
       │                                     ├── MARCA ──────► MARCA_001 (Tipo de Papel / Matéria)
       │                                     └── ETIQUETA ───► ETQ_PROD_001 (Medida / Formato)
       ├── NATUREZA ──────────────────────► NATUREZA_001 (Tipo fiscal: V, B, R, D, etc.)
       ├── QTDE (Quantidade em aberto / pendente)
       ├── QTDE_F (Quantidade já faturada)
       ├── QTDE_CANC (Quantidade cancelada)
       ├── PRECO (Preço unitário)
       └── OBSERVACAO (Observação do item)
```

### 2.1. Dicionário das Principais Colunas

| Tabela | Coluna | Tipo / Significado |
|---|---|---|
| `PEDIDO_001` | `NUMERO` | Número identificador do pedido. |
| `PEDIDO_001` | `DT_EMISSAO` | Data de inclusão/emissão do pedido. Base principal dos filtros temporais. |
| `PEDIDO_001` | `ENTREGA` | Data prometida para entrega do pedido. |
| `PEDIDO_001` | `PERIODO` | Código do prazo de entrega (tabela `TABPRZ_001`). |
| `PEDIDO_001` | `RESPONSAVEL` | Código do responsável pela conta comercial (`ENTIDADE_001`). |
| `PEDIDO_001` | `CODREP` | Código do representante externo (`REPRESEN_001`). |
| `PED_ITEN_001` | `QTDE` | Quantidade pendente / não faturada do item. |
| `PED_ITEN_001` | `QTDE_F` | Quantidade faturada do item (já gerou NF). |
| `PED_ITEN_001` | `QTDE_CANC` | Quantidade cancelada do item. |
| `PED_ITEN_001` | `PRECO` | Preço unitário contratado no pedido. |
| `PED_ITEN_001` | `NATUREZA` | CFOP / Natureza de operação do item. |

---

## 3. Regras de Negócio e Fórmulas de Cálculo

### 3.1. Quantidade Total e Valor do Item
No Sisplan, a quantidade originalmente vendida está fracionada entre o que já foi faturado, cancelado ou ainda aguarda faturamento:

$$\text{QTDE\_TOTAL} = \text{PI.QTDE} + \text{PI.QTDE\_CANC} + \text{PI.QTDE\_F}$$

$$\text{VALOR\_ITEM} = \text{QTDE\_TOTAL} \times \text{COALESCE}(\text{PI.PRECO}, 0)$$

> [!NOTE]
> No Firebird, faça `CAST(PI.QTDE AS DOUBLE PRECISION)` antes de somar para evitar overflow e garantir precisão decimal correta.

---

### 3.2. Classificação da Situação do Item
```sql
CASE 
  WHEN PI.QTDE_F > 0 THEN 'Faturado'
  WHEN PI.QTDE_CANC > 0 THEN 'Cancelado'
  ELSE 'Pendente'
END AS SITUACAO
```
- **Pendente**: `QTDE > 0` e `QTDE_F = 0` e `QTDE_CANC = 0`.
- **Faturado**: Quando o item já foi emitido em Nota Fiscal.
- **Cancelado**: Quando a quantidade foi cancelada pelo comercial ou cliente.

---

### 3.3. Tipo do Item do Pedido (via `NATUREZA_001.TIPO`)
```sql
CASE 
  WHEN NAT.TIPO = 'B' THEN 'Bonificação'
  WHEN NAT.TIPO = 'R' THEN 'Remessa'
  WHEN NAT.TIPO = 'D' THEN 'Devolução'
  WHEN NAT.TIPO = 'O' THEN 'Retorno'
  WHEN NAT.TIPO = 'S' THEN 'Serviço'
  ELSE 'Normal' 
END AS TIPO_ITEM_PEDIDO
```

---

### 3.4. Filtros Obrigatórios de Negócio (Desconsiderações)

Para que o novo programa chegue aos **mesmos valores** do relatório oficial:

1. 🚫 **Desconsiderar entregas recorrentes de estoque (`98 - JIT`)**:
   - **Regra**: Se `CONDICAO_ENTREGA` contiver `"98 - JIT"`, o item **deve ser ignorado**.
   - **Manter**: `"05 - JIT IMEDIATO"` entra na extração (é venda válida do período).
   - **Motivo**: `98 - JIT` são puxadas automáticas de estoque já negociado anteriormente, não representam novas vendas do período.
2. 🚫 **Desconsiderar matrizes gráficas ("CLICHES")**:
   - **Regra**: Se `GRUPO` do produto contiver `"CLICHES"`, o item **deve ser ignorado**.
   - **Motivo**: Clichês são custos gráficos/ferramentais de impressão, não sacolas ou embalagens comercializáveis.
3. 🚫 **Desconsiderar itens com todas as quantidades zeradas**:
   - Cláusula SQL: `AND (PI.QTDE_F <> 0 OR PI.QTDE <> 0 OR PI.QTDE_CANC <> 0)`.
4. 🚫 **Itens Cancelados (quando em relatórios de pendentes ou filtrados)**:
   - Se o objetivo for analisar itens a produzir ou faturar, filtre `SITUACAO <> 'Cancelado'` ou `PI.QTDE_CANC = 0`.

---

### 3.5. Regra de Negócio: Pedidos Filtrados por Responsável
No relatório executivo de pedidos filtrados (`pedidosfiltrados.csv`), além dos filtros de JIT, Clichês e Cancelados, aplica-se uma restrição de carteira aos seguintes 5 responsáveis autorizados:

```python
ALLOWED_RESPONSAVEIS = {
    "13309 - MARIA CECILIA VOLPATO DE FREITAS",
    "05975 - ASSOCIACAO NACIONAL DE FARMACEUTICOS MAGISTRAIS ANFARMA",
    "09044 - AMANDA LONGARAI KOSSMAN",
    "12667 - JOSEANE VOLINGER DOS SANTOS",
    "13242 - CONSULFARMA 2026",
}
```

---

### 3.6. Lógica de Período: Visão Diária vs. Acumulado do Mês

- **Visão Diária (Cards de KPIs)**:
  - Filtra pedidos onde `DT_EMISSAO` é igual à data alvo (ex: ontem ou última sexta-feira).
  - Métricas calculadas:
    - **Total de Clientes Distintos**: Quantidade única de clientes com pedidos no dia.
    - **Quantidade de Itens no Dia**: Soma de `QTDE_TOTAL` dos itens do dia.
    - **Valor Total no Dia**: Soma de `VALOR_ITEM` dos itens do dia.
- **Visão Mensal (Breakdowns)**:
  - Período de busca: Do primeiro dia do mês (`YYYY-MM-01`) até a data alvo (`YYYY-MM-DD`).
  - Agrupamentos executivos calculados:
    1. **Por Categoria (Grupo de Produto)**: `GRUPO_PA.DESCRICAO` (Itens e Valor Total no Mês).
    2. **Por Estado e Cidade**: `UF` e `CIDADE` (Valor Total no Mês ordenado por Estado e Cidade).
    3. **Por Responsável Comercial**: `RESPONSAVEL` (Itens e Valor Total no Mês).

---

## 4. Query SQL Oficial de Extração de Pedidos

A query abaixo é a consulta completa homologada que deve ser utilizada:

```sql
SELECT
    CASE 
      WHEN PI.QTDE_F > 0 THEN 'Faturado'
      WHEN PI.QTDE_CANC > 0 THEN 'Cancelado'
      ELSE 'Pendente'
    END AS SITUACAO,
    CASE 
      WHEN NAT.TIPO = 'B' THEN 'Bonificação'
      WHEN NAT.TIPO = 'R' THEN 'Remessa'
      WHEN NAT.TIPO = 'D' THEN 'Devolução'
      WHEN NAT.TIPO = 'O' THEN 'Retorno'
      WHEN NAT.TIPO = 'S' THEN 'Serviço'
      ELSE 'Normal' 
    END AS TIPO_ITEM_PEDIDO,
    P.PERIODO || ' - ' || COALESCE(T.DESCRICAO, '') AS CONDICAO_ENTREGA,
    P.MOTIVO || ' - ' || COALESCE(M.DESCRICAO, '') AS MOTIVO,
    P.NUMERO AS PEDIDO,
    E.FANTASIA,
    P.DT_EMISSAO AS EMISSAO_PEDIDO,
    FORMATO.DESCRICAO AS MEDIDA,
    ((CAST(PI.QTDE AS DOUBLE PRECISION) + CAST(PI.QTDE_CANC AS DOUBLE PRECISION) + CAST(PI.QTDE_F AS DOUBLE PRECISION)) * COALESCE(PI.PRECO, 0)) AS VALOR_ITEM,
    (CAST(PI.QTDE AS DOUBLE PRECISION) + CAST(PI.QTDE_CANC AS DOUBLE PRECISION) + CAST(PI.QTDE_F AS DOUBLE PRECISION)) AS QTDE_TOTAL,
    P.ENTREGA AS ENTREGA_PEDIDO,
    P.CODREP || ' - ' || COALESCE(REP.NOME, '') AS REPRESENTANTE,
    P.RESPONSAVEL || ' - ' || COALESCE(PES.NOME, '') AS RESPONSAVEL,
    CITY.NOME AS CIDADE,
    PAPEL.DESCRICAO AS PAPEL,
    GRUPO_PA.DESCRICAO AS GRUPO,
    P.CODCLI || ' - ' || COALESCE(E.NOME, '') AS CLIENTE,
    (SELECT FIRST 1 PII.OBSERVACAO FROM PED_ITEN_001 PII WHERE PII.NUMERO = PI.NUMERO AND PII.CODIGO = PI.CODIGO) AS OBS,
    CITY.COD_UF AS UF,
    PI.PRECO,
    PED_ITEN.VALOR_PED,
    PROD.CODIGO AS CODIGO,
    PROD.DESCRICAO AS DESCRICAO,
    E.DDD_FONE AS DDD,
    E.TELEFONE AS TELEFONE,
    E.EMAIL AS EMAIL
FROM PED_ITEN_001 PI 
INNER JOIN PEDIDO_001 P ON (P.NUMERO = PI.NUMERO)
INNER JOIN PRODUTO_001 PROD ON (PROD.CODIGO = PI.CODIGO)
LEFT JOIN NATUREZA_001 NAT ON (NAT.NATUREZA = PI.NATUREZA)
LEFT JOIN ENTIDADE_001 E ON (E.CODCLI = P.CODCLI)
LEFT JOIN CADCEP_001 CEP ON (CEP.CEP = E.CEP)
LEFT JOIN CIDADE CITY ON (CEP.CODMUN = CITY.CODIGO)
LEFT JOIN GRUPO_PA_001 GRUPO_PA ON (GRUPO_PA.CODIGO = PROD.GRUPO)
LEFT JOIN MARCA_001 PAPEL ON (PAPEL.CODIGO = PROD.MARCA)
LEFT JOIN ETQ_PROD_001 FORMATO ON (FORMATO.CODIGO = PROD.ETIQUETA)
LEFT JOIN SITPROD_001 STATUS ON (STATUS.CODIGO = P.STATUS)
LEFT JOIN REPRESEN_001 REP ON (REP.CODREP = P.CODREP)
LEFT JOIN ENTIDADE_001 PES ON (PES.CODCLI = P.RESPONSAVEL)
LEFT JOIN MENSAGEM_001 M ON (M.CODMEN = P.MOTIVO)
LEFT JOIN (
    SELECT NUMERO, SUM((CAST(QTDE AS DOUBLE PRECISION) + CAST(QTDE_CANC AS DOUBLE PRECISION) + CAST(QTDE_F AS DOUBLE PRECISION)) * COALESCE(PRECO, 0)) AS VALOR_PED 
    FROM PED_ITEN_001 
    GROUP BY NUMERO
) PED_ITEN ON (PED_ITEN.NUMERO = P.NUMERO)
LEFT JOIN TABPRZ_001 T ON (T.PRAZO = P.PERIODO)
WHERE P.DT_EMISSAO BETWEEN ? AND ?
  AND (PI.QTDE_F <> 0 OR PI.QTDE <> 0 OR PI.QTDE_CANC <> 0)
  AND TRIM(COALESCE(P.PERIODO, '')) <> '98'
  AND UPPER(P.PERIODO || ' - ' || COALESCE(T.DESCRICAO, '')) NOT LIKE '%98 - JIT%'
  AND UPPER(COALESCE(GRUPO_PA.DESCRICAO, '')) NOT LIKE '%CLICHES%'
ORDER BY P.NUMERO;
```

---

## 5. Exemplo de Código de Conexão e Processamento em Python

```python
import os
import fdb
from pathlib import Path
from datetime import date, timedelta

BASE_DIR = Path(r"C:\dev\auto_sisplan")
FB_DLL = BASE_DIR / "fbclient_x64.dll"

# 1. Carregar a biblioteca client 64-bit do Firebird 3
if FB_DLL.exists():
    fdb.load_api(str(FB_DLL))

def extrair_pedidos(dt_inicio: str, dt_fim: str):
    # Conexão em modo estrito de leitura
    conn = fdb.connect(
        host="192.168.100.168",
        port=3051,
        database=r"C:\Sisplan\Base\14215F.FDB",
        user="SYSDBA",
        password="masterkey"
    )
    
    cur = conn.cursor()
    # Execute a query da Seção 4 passando (dt_inicio, dt_fim)
    with open(BASE_DIR / "sqlcodes" / "pedidos_completo.sql", "r", encoding="utf-8") as f_sql:
        sql = f_sql.read()
        
    cur.execute(sql, (dt_inicio, dt_fim))
    rows = cur.fetchall()
    
    pedidos_validos = []
    for r in rows:
        condicao_entrega = (r[2] or "").strip()
        grupo = (r[15] or "").strip()
        
        # Aplicar regras de exclusão de negócio
        if "98 - JIT" in condicao_entrega.upper():
            continue
        if "CLICHES" in grupo.upper():
            continue
            
        pedidos_validos.append(r)
        
    conn.close()
    return pedidos_validos
```

---

## 6. Checklist de Implementação para o Novo Programa

- [ ] **Configuração de Rede**: Conectar ao IP `192.168.100.168` na porta `3051`.
- [ ] **Modo de Conexão**: Abertura em `READ-ONLY` / transação de apenas leitura.
- [ ] **Biblioteca**: `fdb` com DLL de 64 bits (`fbclient_x64.dll`).
- [ ] **Cálculo de Quantidade e Valor**: Somar `QTDE + QTDE_CANC + QTDE_F` e multiplicar por `PRECO`.
- [ ] **Exclusões**: Descartar `98 - JIT` e produtos do grupo `CLICHES`. Manter `05 - JIT IMEDIATO`.
- [ ] **Data de Referência**: Para rotinas automáticas de dia útil anterior, lembrar de converter Segunda-feira para a Sexta-feira anterior (`dt - timedelta(days=3)`).
