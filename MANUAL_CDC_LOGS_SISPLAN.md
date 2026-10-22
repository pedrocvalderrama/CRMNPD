# Manual Técnico: Monitoramento Reativo de Logs do Sisplan (CDC — Change Data Capture)

> **Documento Técnico de Referência** para integração direta com o banco de dados Firebird do ERP Sisplan através das tabelas nativas de auditoria e log.
> **Público-alvo:** Desenvolvedores e agentes de IA que precisam sincronizar dados do Sisplan para sistemas locais ou bancos de dados próprios de forma contínua, eficiente e de baixo impacto no servidor.

---

## 1. Por que usar CDC em vez de Polling Contínuo?

No modelo tradicional de polling (consultas periódicas a cada 5 minutos):
* O sistema executa queries pesadas (`SELECT * FROM ENTIDADE_001`, `SELECT * FROM PRODUTO_001`) comparando hashes ou timestamps.
* Causa uso desnecessário de I/O em disco no servidor do ERP.
* Gera atraso de até 5 minutos para os dados chegarem ao destino.

No modelo **CDC (Change Data Capture)** via tabelas de log do Sisplan:
* O sistema consulta **apenas IDs numéricos incrementais** (`WHERE ID > last_id`) nas tabelas de auditoria do próprio ERP.
* O custo de cada ciclo de verificação é de **menos de 2 milissegundos** e trafega apenas alguns bytes.
* O intervalo de verificação pode ser reduzido para **3 a 5 segundos**, oferecendo uma sincronização quase em **tempo real** sem sobrecarregar o Firebird.
* O sistema opera em modo **estritamente de leitura (`READ-ONLY`)**, sem triggers customizadas, sem modificar tabelas do ERP e com risco zero de bloqueio (lock).

---

## 2. As Tabelas de Log do Sisplan

O ERP Sisplan possui um mecanismo nativo que audita as alterações executadas pelos usuários do sistema. As duas tabelas centrais utilizadas para o monitoramento são:

### 2.1. Tabela `LOG_ALTERACAO` (Auditoria de Modificações / UPDATEs)
Registra alterações campo a campo feitas nos registros.

| Coluna | Tipo | Descrição |
| :--- | :--- | :--- |
| **`ID`** | `INTEGER` / `BIGINT` | Chave primária incremental. É o cursor principal de controle. |
| **`TABELA`** | `VARCHAR` | Nome da tabela do Firebird modificada (ex: `ENTIDADE_001`, `PRODUTO_001`, `PEDIDO_001`). |
| **`TELA`** | `VARCHAR` | Nome ou código da tela do Sisplan onde a alteração foi feita. |
| **`CAMPO_CHAVE`** | `VARCHAR` | Expressão indicando a chave primária do registro (ex: `CODCLI = 13598` ou `CODIGO = 02.03.07.0090`). |
| **`CAMPO`** | `VARCHAR` | Nome da coluna que sofreu a alteração (ex: `FANTASIA`, `TELEFONE`, `PRECO`). |
| **`VALOR_ANTERIOR`** | `VARCHAR` | Conteúdo anterior antes da gravação. |
| **`VALOR_NOVO`** | `VARCHAR` | Novo conteúdo gravado. |
| **`DATA_HORA`** / **`DATA`** | `TIMESTAMP` / `DATE` | Data e hora em que a modificação ocorreu. |
| **`USUARIO`** | `VARCHAR` | Usuário do Sisplan responsável pela alteração. |

### 2.2. Tabela `LOG` (Auditoria Geral e Novos Cadastros / INSERTs)
Registra eventos gerais do sistema, principalmente novas inclusões de registros.

| Coluna | Tipo | Descrição |
| :--- | :--- | :--- |
| **`ID`** | `INTEGER` / `BIGINT` | Chave primária incremental. |
| **`TELA`** | `VARCHAR` | Nome descritivo da tela (ex: `CADASTRO DE CLIENTE`, `CADASTRO DE PRODUTO`, `ENTIDADE`). |
| **`OPERACAO`** | `VARCHAR` | Tipo de operação executada (ex: `INCLUSAO`, `ALTERACAO`, `EXCLUSAO`). |
| **`CHAVE`** | `VARCHAR` | Identificador direto do registro criado ou manipulado (ex: o `CODCLI` ou o `CODIGO`). |
| **`DATA`** / **`HORA`** | `DATE` / `TIME` | Momento da inclusão. |
| **`USUARIO`** | `VARCHAR` | Nome do usuário operador. |

---

## 3. Arquitetura do Mecanismo de Checkpoint (Cursor)

Para garantir que nenhum registro seja perdido e que nada seja reprocessado desnecessariamente, o processo mantém um arquivo de **Checkpoint local** (ex: `.checkpoint_cdc.json` ou registro em banco local SQLite):

```json
{
  "last_log_alteracao_id": 1425890,
  "last_log_id": 850420,
  "ultima_atualizacao": "2026-10-22 09:20:00"
}
```

### Regras do Cursor:
1. **Cold Start (Inicialização do Zero):**
   Se o arquivo de checkpoint não existir, o sistema NÃO deve ler todos os logs do início dos tempos. Ele executa:
   ```sql
   SELECT MAX(ID) FROM LOG_ALTERACAO;
   SELECT MAX(ID) FROM LOG;
   ```
   E salva esses valores máximos no checkpoint. A partir desse instante, passa a monitorar tudo que for maior (`ID > last_id`). *(Se o objetivo for uma carga inicial completa dos dados, ela deve ser feita via SELECT na tabela principal uma única vez)*.

2. **Avanço Atômico do Cursor:**
   O cursor **só deve ser atualizado e salvo após o processamento com sucesso** no sistema de destino. Se o sistema falhar ao processar uma alteração, o checkpoint antigo permanece intacto e a mesma alteração será reprocessada no próximo ciclo.

3. **Escrita Segura em Disco:**
   Para evitar corrupção em caso de queda de energia ou desligamento abrupto, escreva sempre em um arquivo temporário (`.tmp`) e faça a substituição atômica (`replace`/`rename`).

---

## 4. O Ciclo de Execução (Algoritmo Passo a Passo)

A cada ciclo (ex: a cada 3 a 5 segundos):

```
┌────────────────────────────────────────────────────────────────────────┐
│ 1. Ler checkpoint atual (last_alt_id, last_log_id)                     │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 2. Executar SELECT em LOG_ALTERACAO WHERE ID > last_alt_id             │
│    Executar SELECT em LOG           WHERE ID > last_log_id             │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 3. Há novos registros?                                                 │
│    • NÃO ➔ Se os IDs avançaram (outras tabelas), atualiza cursor. Fim. │
│    • SIM ➔ Extrair chaves (CODCLI, CODIGO do produto, etc.) em um Set  │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 4. Deduplicar chaves (Buffer / Debounce em memória)                    │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 5. Buscar dados atualizados via SELECT direto na tabela correspondente │
│    WHERE CODCLI IN (...) ou WHERE CODIGO IN (...)                      │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 6. Atualizar a base de dados do seu projeto local                      │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 7. Sucesso? ➔ Salvar novo last_alt_id e last_log_id no checkpoint      │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 5. Extração e Parsing das Chaves nos Logs

Como os campos de log contêm formatos mistos gerados pelas telas do Sisplan, deve-se usar Expressões Regulares (Regex) e splits seguros:

### 5.1. Extraindo de `LOG_ALTERACAO`
O campo `CAMPO_CHAVE` costuma vir no formato `CAMPO = VALOR`.

* **Clientes (`ENTIDADE` / `ENTIDADE_001`):**
  ```python
  if "ENTIDADE" in tabela or "ENTIDADE" in tela:
      match = re.search(r"CODCLI\s*=\s*([0-9A-Za-z\.\-_/]+)", campo_chave, re.IGNORECASE)
      if match:
          codcli = match.group(1).strip()
          conjunto_clientes.add(codcli)
  ```

* **Produtos (`PRODUTO` / `PRODUTO_001`):**
  ```python
  if "PRODUTO" in tabela or "PRODUTO" in tela:
      match = re.search(r"CODIGO\s*=\s*([0-9A-Za-z\.\-_/]+)", campo_chave, re.IGNORECASE)
      if match:
          codigo = match.group(1).strip()
          conjunto_produtos.add(codigo)
  ```

* **Pedidos de Venda (`PEDIDO_001`):**
  ```python
  if "PEDIDO" in tabela or "PEDIDO" in tela:
      match = re.search(r"PED_NUM\s*=\s*(\d+)", campo_chave, re.IGNORECASE)
      if match:
          ped_num = match.group(1).strip()
          conjunto_pedidos.add(ped_num)
  ```

### 5.2. Extraindo de `LOG`
O campo `CHAVE` costuma conter o identificador diretamente ou separado por espaços.

* **Clientes:**
  ```python
  if any(t in tela for t in ("CADASTRO DE CLIENTE", "ENTIDADE")):
      clean_codcli = re.split(r"[\s,;]", chave)[0].strip()
      if clean_codcli:
          conjunto_clientes.add(clean_codcli)
  ```

* **Produtos:**
  ```python
  if any(t in tela for t in ("CADASTRO DE PRODUTO", "PRODUTO")):
      clean_codigo = re.split(r"[\s,;]", chave)[0].strip()
      if clean_codigo:
          conjunto_produtos.add(clean_codigo)
  ```

---

## 6. Consultas Diretas aos Cadastros (Fetch Otimizado)

Depois que você tem o conjunto de chaves únicas que sofreram alteração (ex: `['1020', '13598']`), basta fazer uma busca pontual e rápida por índice.

### 6.1. Consulta de Clientes
```sql
SELECT 
    TRIM(E.CODCLI) AS CODCLI,
    TRIM(COALESCE(E.FANTASIA, E.NOME)) AS FANTASIA,
    TRIM(E.NOME) AS RAZAO_SOCIAL,
    TRIM(COALESCE(E.CNPJ, '')) AS CNPJ,
    TRIM(COALESCE(E.INSCRICAO, 'ISENTO')) AS INSCRICAO,
    TRIM(COALESCE(E.ENDERECO, '')) AS ENDERECO,
    TRIM(COALESCE(E.NUMERO, 'S/N')) AS NUMERO,
    TRIM(COALESCE(E.BAIRRO, '')) AS BAIRRO,
    TRIM(COALESCE(E.CEP, '')) AS CEP,
    TRIM(COALESCE(CITY.NOME, '')) AS CIDADE,
    TRIM(COALESCE(CITY.COD_UF, '')) AS UF,
    TRIM(COALESCE(E.DDD_FONE, '')) || ' ' || TRIM(COALESCE(E.TELEFONE, '')) AS TELEFONE,
    TRIM(COALESCE(E.EMAIL, '')) AS EMAIL,
    TRIM(COALESCE(E.CODREP, '')) AS CODREP,
    COALESCE(E.ID_TIPO, 18) AS ID_TIPO,
    TRIM(COALESCE(E.ATIVO, 'S')) AS ATIVO
FROM ENTIDADE_001 E
LEFT JOIN CADCEP_001 CEP ON (CEP.CEP = E.CEP)
LEFT JOIN CIDADE CITY ON (CEP.CODMUN = CITY.CODIGO)
WHERE E.CODCLI IN (?, ?, ...);
```

### 6.2. Consulta de Produtos
```sql
SELECT 
    TRIM(P.CODIGO) AS CODIGO,
    TRIM(P.DESCRICAO) AS DESCRICAO,
    TRIM(COALESCE(G.DESCRICAO, 'Geral')) AS CATEGORIA,
    TRIM(COALESCE(P.UNIDADE, 'UN')) AS UNIDADE,
    TRIM(COALESCE(P.ATIVO, 'S')) AS ATIVO
FROM PRODUTO_001 P
LEFT JOIN GRUPO_PA_001 G ON (G.CODIGO = P.GRUPO)
WHERE P.CODIGO IN (?, ?, ...);
```

---

## 7. Implementação Completa de Referência (Python)

Abaixo está o script completo e autossuficiente que o agente do outro projeto pode utilizar diretamente ou traduzir para outra linguagem (Node.js, C#, Go):

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script de Monitoramento CDC via Logs do Sisplan (Firebird)
"""

import sys
import os
import re
import time
import json
import logging
from pathlib import Path
from datetime import datetime

# Biblioteca oficial para Firebird 2.5 / 3.0
import fdb

# -----------------------------------------------------------------------------
# Configurações do Banco e do Cursor
# -----------------------------------------------------------------------------
FIREBIRD_CONFIG = {
    "host": "192.168.100.168",
    "port": 3051,
    "database": r"C:\Sisplan\Base\14215F.FDB",
    "user": "SYSDBA",
    "password": "masterkey",
    "charset": "WIN1252",
    "client_dll": r"C:\dev\auto_sisplan\fbclient_x64.dll" # Opcional se já estiver no PATH
}

CHECKPOINT_FILE = Path(".checkpoint_cdc.json")
POLL_INTERVAL_SEGUNDOS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("CDC_Sisplan")


# -----------------------------------------------------------------------------
# Conexão Segura
# -----------------------------------------------------------------------------
def conectar_firebird():
    client_dll = Path(FIREBIRD_CONFIG.get("client_dll", ""))
    if client_dll.exists():
        try:
            fdb.load_api(str(client_dll))
        except Exception as e:
            logger.debug(f"Aviso ao carregar fbclient: {e}")

    return fdb.connect(
        host=FIREBIRD_CONFIG["host"],
        port=FIREBIRD_CONFIG["port"],
        database=FIREBIRD_CONFIG["database"],
        user=FIREBIRD_CONFIG["user"],
        password=FIREBIRD_CONFIG["password"],
        charset=FIREBIRD_CONFIG["charset"]
    )


# -----------------------------------------------------------------------------
# Gestão do Checkpoint (Cursor)
# -----------------------------------------------------------------------------
def carregar_checkpoint(conn) -> dict:
    if CHECKPOINT_FILE.exists():
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Erro ao ler checkpoint. Inicializando com estado atual: {e}")

    # Cold Start: Pega o MAX(ID) atual para começar a monitorar a partir de agora
    cur = conn.cursor()
    cur.execute("SELECT MAX(ID) FROM LOG_ALTERACAO")
    max_alt = cur.fetchone()[0] or 0

    cur.execute("SELECT MAX(ID) FROM LOG")
    max_log = cur.fetchone()[0] or 0

    checkpoint = {
        "last_log_alteracao_id": int(max_alt),
        "last_log_id": int(max_log),
        "inicializado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    salvar_checkpoint(checkpoint)
    logger.info(f"Cursor inicializado no estado atual: {checkpoint}")
    return checkpoint


def salvar_checkpoint(checkpoint: dict):
    temp = CHECKPOINT_FILE.with_suffix(".tmp")
    checkpoint["ultima_atualizacao"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f, indent=2)
    temp.replace(CHECKPOINT_FILE)


# -----------------------------------------------------------------------------
# Leitura Incremental dos Logs
# -----------------------------------------------------------------------------
def detectar_mudancas(conn, checkpoint: dict):
    cur = conn.cursor()
    clientes = set()
    produtos = set()

    last_alt = checkpoint.get("last_log_alteracao_id", 0)
    last_log = checkpoint.get("last_log_id", 0)

    novo_alt = last_alt
    novo_log = last_log

    # 1. Consulta atualizações em LOG_ALTERACAO
    cur.execute("""
        SELECT ID, TABELA, TELA, CAMPO_CHAVE 
        FROM LOG_ALTERACAO 
        WHERE ID > ? 
        ORDER BY ID ASC
    """, (last_alt,))

    for row in cur.fetchall():
        r_id, tabela, tela, chave = row[0], str(row[1] or "").upper(), str(row[2] or "").upper(), str(row[3] or "")
        novo_alt = max(novo_alt, r_id)

        if "ENTIDADE" in tabela or "ENTIDADE" in tela:
            m = re.search(r"CODCLI\s*=\s*([0-9A-Za-z\.\-_/]+)", chave, re.IGNORECASE)
            if m:
                clientes.add(m.group(1).strip())

        elif "PRODUTO" in tabela or "PRODUTO" in tela:
            m = re.search(r"CODIGO\s*=\s*([0-9A-Za-z\.\-_/]+)", chave, re.IGNORECASE)
            if m:
                produtos.add(m.group(1).strip())

    # 2. Consulta inclusões em LOG
    cur.execute("""
        SELECT ID, TELA, OPERACAO, CHAVE 
        FROM LOG 
        WHERE ID > ? 
        ORDER BY ID ASC
    """, (last_log,))

    for row in cur.fetchall():
        r_id, tela, operacao, chave = row[0], str(row[1] or "").upper(), str(row[2] or "").upper(), str(row[3] or "").strip()
        novo_log = max(novo_log, r_id)

        if not chave:
            continue

        if any(term in tela for term in ("CADASTRO DE CLIENTE", "ENTIDADE")):
            clean_codcli = re.split(r"[\s,;]", chave)[0]
            if clean_codcli:
                clientes.add(clean_codcli)

        elif any(term in tela for term in ("CADASTRO DE PRODUTO", "PRODUTO")):
            clean_codigo = re.split(r"[\s,;]", chave)[0]
            if clean_codigo:
                produtos.add(clean_codigo)

    return clientes, produtos, novo_alt, novo_log


# -----------------------------------------------------------------------------
# Hook de Processamento no Seu Projeto Local
# -----------------------------------------------------------------------------
def processar_mudancas_no_sistema_local(conn, codclis: set, produtos_cods: set) -> bool:
    """
    Aqui o seu sistema local executa o que for necessário com os dados alterados.
    Exemplo: Atualizar um banco local SQLite, PostgreSQL, MongoDB, Redis, etc.
    """
    try:
        cur = conn.cursor()

        # Se clientes mudaram, busca os dados completos no Firebird
        if codclis:
            placeholders = ",".join(["?"] * len(codclis))
            cur.execute(f"""
                SELECT TRIM(CODCLI), TRIM(COALESCE(FANTASIA, NOME)), TRIM(COALESCE(CNPJ, ''))
                FROM ENTIDADE_001
                WHERE CODCLI IN ({placeholders})
            """, tuple(codclis))
            
            for cli in cur.fetchall():
                cod, nome, cnpj = cli[0], cli[1], cli[2]
                logger.info(f"[PROJETO LOCAL] Atualizando Cliente: {cod} - {nome} (CNPJ: {cnpj})")
                # ---> SEU CÓDIGO DE UPDATE NO BANCO LOCAL AQUI <---

        # Se produtos mudaram, busca os dados completos no Firebird
        if produtos_cods:
            placeholders = ",".join(["?"] * len(produtos_cods))
            cur.execute(f"""
                SELECT TRIM(CODIGO), TRIM(DESCRICAO), TRIM(COALESCE(UNIDADE, 'UN'))
                FROM PRODUTO_001
                WHERE CODIGO IN ({placeholders})
            """, tuple(produtos_cods))
            
            for prod in cur.fetchall():
                cod, desc, un = prod[0], prod[1], prod[2]
                logger.info(f"[PROJETO LOCAL] Atualizando Produto: {cod} - {desc} ({un})")
                # ---> SEU CÓDIGO DE UPDATE NO BANCO LOCAL AQUI <---

        return True

    except Exception as err:
        logger.error(f"Erro ao processar alterações no sistema local: {err}")
        return False


# -----------------------------------------------------------------------------
# Loop Principal de Execução Contínua
# -----------------------------------------------------------------------------
def main():
    logger.info("Iniciando serviço de monitoramento CDC do Sisplan...")

    conn = None
    while True:
        try:
            if conn is None:
                logger.info("Conectando ao banco Firebird do Sisplan...")
                conn = conectar_firebird()
                logger.info("Conexão estabelecida com sucesso.")

            checkpoint = carregar_checkpoint(conn)
            clientes_ids, produtos_ids, novo_alt, novo_log = detectar_mudancas(conn, checkpoint)

            # Se houve alguma alteração de cliente ou produto
            if clientes_ids or produtos_ids:
                logger.info(f"[CDC Detectado] Clientes: {len(clientes_ids)}, Produtos: {len(produtos_ids)}")
                
                sucesso = processar_mudancas_no_sistema_local(conn, clientes_ids, produtos_ids)

                if sucesso:
                    checkpoint["last_log_alteracao_id"] = novo_alt
                    checkpoint["last_log_id"] = novo_log
                    salvar_checkpoint(checkpoint)
                    logger.info(f"[OK] Checkpoint avançado para ALT={novo_alt}, LOG={novo_log}")
                else:
                    logger.warning("[Alerta] Falha ao gravar no projeto local. O cursor não foi avançado e tentará novamente.")

            else:
                # Se apenas outras tabelas não monitoradas geraram logs, avança o cursor silenciosamente
                if novo_alt != checkpoint["last_log_alteracao_id"] or novo_log != checkpoint["last_log_id"]:
                    checkpoint["last_log_alteracao_id"] = novo_alt
                    checkpoint["last_log_id"] = novo_log
                    salvar_checkpoint(checkpoint)

            # Efetua commit da transação de leitura para liberar garbage collection do Firebird
            conn.commit()

        except fdb.Error as fb_err:
            logger.error(f"Erro no Firebird: {fb_err}. Reconectando em 5 segundos...")
            conn = None
        except Exception as e:
            logger.error(f"Erro inesperado no loop CDC: {e}")

        time.sleep(POLL_INTERVAL_SEGUNDOS)


if __name__ == "__main__":
    main()
```

---

## 8. Dicas Importantes para o Desenvolvedor do Outro Projeto

1. **Commit na Transação de Leitura:**
   * No Firebird, transações abertas por muito tempo impedem o *Garbage Collection* interno do banco e causam crescimento de memória.
   * Sempre execute `conn.commit()` após cada ciclo de verificação, mesmo que apenas leituras tenham sido feitas.

2. **Reconexão Automática:**
   * Se o servidor do ERP for reiniciado ou oscilar na rede, o objeto `conn` deve ser descartado (`conn = None`) para forçar um novo `fdb.connect()` no ciclo seguinte.

3. **Multi-plataforma e Arquitetura 32/64 bits:**
   * Se o seu Python for de 64 bits, a DLL do Firebird (`fbclient.dll`) **obrigatoriamente deve ser a de 64 bits**.
   * Se for usar em Linux / Docker, instale o pacote `libfbclient2` e use o charset `WIN1252`.

4. **Extensão para Outras Tabelas:**
   * Caso queira escutar pedidos de venda, ordens de produção ou notas fiscais, basta adicionar as tabelas no bloco `if tabela == 'PEDIDO_001'` ou `if 'PEDIDO' in tela`.
