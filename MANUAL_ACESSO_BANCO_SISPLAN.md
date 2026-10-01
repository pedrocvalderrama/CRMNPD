# Manual Geral de Acesso ao Banco de Dados ERP Sisplan (Firebird)

Este documento é o **guia oficial e obrigatório** de integração e acesso ao banco de dados em produção do **ERP Sisplan**. Todo e qualquer projeto, script ou assistente de IA (AGY/Antigravity) que precise consultar dados da empresa deve seguir rigorosamente as especificações deste manual.

---

> [!CAUTION]
> ### 🚨 REGRA DE OURO E INVIOLÁVEL: LEITURA ESTRITA (READ-ONLY)
> 
> **O BANCO DE DADOS DO SISPLAN (`14215F.FDB`) É O SISTEMA DE PRODUÇÃO ATIVO DA EMPRESA.**
> 
> - **É ABSOLUTAMENTE PROIBIDO EXECUTAR QUALQUER COMANDO DE ALTERAÇÃO OU ESCRITA**: `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `EXECUTE PROCEDURE`, ETC.
> - Qualquer tentativa de gravação pode **corromper o banco de dados**, quebrar a integridade referencial do ERP ou **paralisar a operação da fábrica e vendas**.
> - **TODAS AS CONEXÕES E QUERIES DEVEM SER ESTRITAMENTE DE LEITURA (`SELECT`)**.
> - Em linguagens como Python, C#, Node.js ou PHP, garanta sempre que as transações sejam abertas com a propriedade `READ ONLY` / `READ COMMITTED`.

---

## 1. Parâmetros Gerais de Conexão com o Banco de Dados

| Parâmetro | Valor de Conexão | Observações / Detalhes |
|---|---|---|
| **Servidor / Host** | `192.168.100.168` (`servidorge`) | Endereço IP do servidor de banco de dados na rede local. |
| **Porta SGBD** | `3051` | **Atenção**: A porta padrão do Firebird é 3050, mas a base `14215F` roda na instância do Firebird 3 na porta **3051**. |
| **Caminho do Banco de Dados** | `C:\Sisplan\Base\14215F.FDB` | Mapeamento no servidor. Em unidades de rede compartilhadas: `V:\Base\14215F.FDB`. |
| **Usuário** | `SYSDBA` | Usuário administrativo padrão do Firebird. |
| **Senha** | `masterkey` | Senha padrão do Firebird. |
| **Client DLL (64-bit)** | `fbclient_x64.dll` | Biblioteca cliente 64-bits necessária para conectar Python 64-bit ao Firebird 3. |
| **Dialeto Firebird** | Dialeto 3 | Suporta SQL ANSI padrão, subqueries, `COALESCE`, `CAST`, `FIRST / SKIP`. |

---

## 2. Visão Geral das Principais Tabelas e Relacionamentos

### 2.1. Faturamento e Notas Fiscais Emitidas
- **`NOTA_001`**: Cabeçalho de Notas Fiscais (`FATURA`, `DT_EMISSAO`, `SITUACAO = 'V'`, `CODCLI`, `CODREP`, `EMP_ID`).
- **`NOTAITEN_001`**: Itens da Nota Fiscal (`FATURA`, `CODIGO`, `QTDE`, `VALOR_LIQ`, `VALOR_FRETE`, `NATUREZA`).
- **`NATUREZA_001`**: Tipos de Operação Fiscais (`NATUREZA`, `TIPO`).
  - `TIPO = 'V'`: Faturamento / Vendas Brutas.
  - `TIPO = 'D'`: Devoluções de Venda.
- **`EMPRESA`**: Cadastro de Unidades/Empresas do Grupo (`EMP_ID`, `EMP_NOME`, `EMP_FANTASIA`).

### 2.2. Pedidos de Vendas
- **`PEDIDO_001`**: Cabeçalho dos Pedidos (`NUMERO`, `DT_EMISSAO`, `CODCLI`, `CODREP`, `RESPONSAVEL`, `PERIODO`, `MOTIVO`, `STATUS`).
- **`PED_ITEN_001`**: Itens do Pedido (`NUMERO`, `CODIGO`, `QTDE`, `QTDE_F`, `QTDE_CANC`, `PRECO`, `NATUREZA`).
  - **Lógica da Situação do Item**:
    - `QTDE_F > 0`: Item Faturado.
    - `QTDE_CANC > 0`: Item Cancelado.
    - `QTDE > 0` e `QTDE_F = 0` e `QTDE_CANC = 0`: **Item Pendente**.
- **`PRODUTO_001`**: Cadastro de Produtos (`CODIGO`, `DESCRICAO`, `MARCA`, `GRUPO`, `ETIQUETA`).
- **`GRUPO_PA_001`**: Categoria do Produto (`GRUPO`, `DESCRICAO`).
- **`MARCA_001`**: Papel / Marca (`MARCA`, `DESCRICAO`).
- **`ETQ_PROD_001`**: Medida / Formato (`ETIQUETA`, `DESCRICAO`).

### 2.3. Contas a Receber e Financeiro
- **`RECEBER_001`**: Títulos e Parcelas a Receber (`NUMERO`, `PARCELA`, `QTDEPARCELAS`, `CODCLI`, `EMP_ID`, `DT_EMISSAO`, `DT_VENCTO`, `VALOR2`, `VALOR_PAGO`, `VAL_DEV`, `STATUS`, `SITUACAO`).
  - **Regra de Filtro Padrão de Duplicatas Válidas**:
    - `STATUS = 'DUPL'`
    - `COALESCE(VAL_DEV, 0) BETWEEN 0.00 AND 0.10`
    - Use a coluna **`VALOR2`** para o valor exato de cada parcela devida/emitida.
- **`TABSIT_001`**: Situação da Cobrança (`CODIGO`, `DESCRICAO`).

### 2.4. Clientes e Representantes
- **`ENTIDADE_001`**: Cadastro Geral de Clientes e Pessoas (`CODCLI`, `NOME`, `FANTASIA`, `DDD_FONE`, `TELEFONE`, `EMAIL`, `CEP`).
- **`REPRESEN_001`**: Vendedores e Representantes (`CODREP`, `NOME`).
- **`CADCEP_001`** & **`CIDADE`**: Cadastro de Endereços e Municípios (`CODMUN`, `NOME`, `COD_UF`).

---

## 3. Exemplos de Consultas SQL Prontas (Apenas Leitura)

### 3.1. Faturamento Líquido (Vendas + Frete - Devoluções)
```sql
SELECT 
    N.DT_EMISSAO AS DT_EMISSAO,
    N.FATURA AS FATURA,
    N.CODREP || ' - ' || COALESCE(REP.NOME, '') AS NOMEREP,
    E.CODCLI || ' - ' || COALESCE(E.NOME, '') AS CLIENTE,
    COALESCE(E.FANTASIA, E.NOME) AS FANTASIA,
    PROD.CODIGO AS CODIGO,
    PROD.DESCRICAO AS DESCRICAO,
    CAST(NI.QTDE AS DOUBLE PRECISION) AS QTDE,
    CAST(NI.VALOR_LIQ AS DOUBLE PRECISION) AS VALOR_LIQ,
    NAT.TIPO AS NAT_TIPO,
    CAST(COALESCE(NI.VALOR_FRETE, 0) AS DOUBLE PRECISION) AS VALOR_FRETE,
    COALESCE(EMP.EMP_FANTASIA, EMP.EMP_NOME, CAST(N.EMP_ID AS VARCHAR(10))) AS EMPRESA_NOME
FROM NOTAITEN_001 NI
INNER JOIN NOTA_001 N ON (N.FATURA = NI.FATURA)
INNER JOIN PRODUTO_001 PROD ON (PROD.CODIGO = NI.CODIGO)
INNER JOIN NATUREZA_001 NAT ON (NAT.NATUREZA = NI.NATUREZA)
LEFT JOIN ENTIDADE_001 E ON (E.CODCLI = N.CODCLI)
LEFT JOIN REPRESEN_001 REP ON (REP.CODREP = N.CODREP)
LEFT JOIN EMPRESA EMP ON (EMP.EMP_ID = N.EMP_ID)
WHERE N.DT_EMISSAO BETWEEN '2026-09-01' AND '2026-09-30'
  AND NAT.TIPO IN ('V', 'D')
  AND (N.MOTIVO_CANCEL IS NULL OR TRIM(N.MOTIVO_CANCEL) = '')
ORDER BY N.FATURA;
```

### 3.2. Pedidos Pendentes de Vendas
```sql
SELECT
    P.NUMERO AS PEDIDO,
    E.FANTASIA,
    P.DT_EMISSAO AS EMISSAO_PEDIDO,
    FORMATO.DESCRICAO AS MEDIDA,
    ((CAST(PI.QTDE AS DOUBLE PRECISION) + CAST(PI.QTDE_CANC AS DOUBLE PRECISION) + CAST(PI.QTDE_F AS DOUBLE PRECISION)) * COALESCE(PI.PRECO, 0)) AS VALOR_ITEM,
    (CAST(PI.QTDE AS DOUBLE PRECISION) + CAST(PI.QTDE_CANC AS DOUBLE PRECISION) + CAST(PI.QTDE_F AS DOUBLE PRECISION)) AS QTDE_TOTAL,
    P.CODREP || ' - ' || COALESCE(REP.NOME, '') AS REPRESENTANTE,
    CITY.NOME AS CIDADE,
    CITY.COD_UF AS UF,
    PROD.CODIGO AS CODIGO,
    PROD.DESCRICAO AS DESCRICAO
FROM PED_ITEN_001 PI 
INNER JOIN PEDIDO_001 P ON (P.NUMERO = PI.NUMERO)
INNER JOIN PRODUTO_001 PROD ON (PROD.CODIGO = PI.CODIGO)
LEFT JOIN NATUREZA_001 NAT ON (NAT.NATUREZA = PI.NATUREZA)
LEFT JOIN ENTIDADE_001 E ON (E.CODCLI = P.CODCLI)
LEFT JOIN CADCEP_001 CEP ON (CEP.CEP = E.CEP)
LEFT JOIN CIDADE CITY ON (CEP.CODMUN = CITY.CODIGO)
LEFT JOIN ETQ_PROD_001 FORMATO ON (FORMATO.CODIGO = PROD.ETIQUETA)
LEFT JOIN REPRESEN_001 REP ON (REP.CODREP = P.CODREP)
WHERE P.DT_EMISSAO BETWEEN '1200-01-01' AND '5000-01-01'
  AND COALESCE(PI.QTDE_F, 0) = 0
  AND COALESCE(PI.QTDE_CANC, 0) = 0
  AND PI.QTDE > 0
ORDER BY P.NUMERO;
```

### 3.3. Contas a Receber (Emissão / Vencimento)
```sql
SELECT 
    R.EMP_ID AS EMPRESA,
    R.NUMERO AS NUM_TITULO,
    R.PARCELA || '/' || R.QTDEPARCELAS AS PARCELA,
    E.NOME AS CLIENTE,
    R.DT_EMISSAO,
    R.DT_VENCTO AS DATA_VENCIMENTO,
    R.VALOR2 AS VALOR_PARCELA,
    COALESCE(R.VALOR_PAGO, 0) AS VALOR_PAGO,
    S.DESCRICAO AS SITUACAO
FROM RECEBER_001 R
LEFT JOIN ENTIDADE_001 E ON (R.CODCLI = E.CODCLI)
LEFT JOIN TABSIT_001 S ON (R.SITUACAO = S.CODIGO)
WHERE R.DT_EMISSAO BETWEEN '2026-09-01' AND '2026-09-30'
  AND COALESCE(R.VAL_DEV, 0) BETWEEN 0.00 AND 0.10
  AND R.STATUS = 'DUPL'
ORDER BY R.DT_EMISSAO, R.NUMERO;
```

---

## 4. Padrão de Conexão em Python (`fdb`)

Sempre utilize a biblioteca oficial `fdb` fornecendo o caminho da DLL de 64-bits `fbclient_x64.dll`:

```python
from pathlib import Path
import fdb

# 1. Diretório base e carregamento da DLL cliente Firebird 3
BASE_DIR = Path(r"C:\dev\auto_sisplan")
fb_dll = BASE_DIR / "fbclient_x64.dll"

if fb_dll.exists():
    fdb.load_api(str(fb_dll))

# 2. Estabelecer conexão READ-ONLY com o Firebird
try:
    conn = fdb.connect(
        host="192.168.100.168",
        port=3051,
        database=r"C:\Sisplan\Base\14215F.FDB",
        user="SYSDBA",
        password="masterkey"
    )
    cur = conn.cursor()
    
    # 3. Executar apenas comandos SELECT
    cur.execute("SELECT FIRST 10 FATURA, DT_EMISSAO, VAL_NOTA FROM NOTA_001 WHERE SITUACAO = 'V'")
    rows = cur.fetchall()
    
    for row in rows:
        print(row)
        
    conn.close()
except Exception as e:
    print(f"Erro na conexão com o banco Sisplan: {e}")
```

---

## 5. Instruções para Assistentes de IA e Novos Projetos (Prompt Directives)

Ao iniciar qualquer projeto ou automação que consulte o Firebird do Sisplan, forneça as seguintes instruções:

1. **Escopo Estritamente de Leitura**: Todas as interações com o banco devem usar unicamente cláusulas `SELECT`. Nunca gerar comandos `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `DROP` ou `ALTER`.
2. **Endereço do Banco**: Host `192.168.100.168`, Porta `3051`, Banco `C:\Sisplan\Base\14215F.FDB`.
3. **Biblioteca de Conexão**: Usar `fdb` em Python apontando para a DLL local `C:\dev\auto_sisplan\fbclient_x64.dll`.
4. **Filtros de Cancelamento e Devoluções**: Respeitar as regras de negócio (`MOTIVO_CANCEL IS NULL` para Notas Fiscais e `STATUS = 'DUPL'` com `VAL_DEV BETWEEN 0.00 AND 0.10` para Contas a Receber).
