# 📘 Manual Técnico: Leitura Incremental em "Tempo Real" por Timestamp (Change Log) — Planilha `notas_ml`

Este manual explica como o **seu outro aplicativo** deve consumir a planilha **`notas_ml`** (aba **`Página1`**) funcionando exatamente como um **Log de Alterações (CDC / Change Data Capture)** baseado em **Timestamp (`Timestamp Log` — Coluna P)** e/ou **Ponteiro de Linha (`since_row`)**.

Dessa forma, o outro aplicativo **não precisa reler a planilha inteira**: ele pergunta apenas *"O que entrou na planilha depois do meu último Timestamp?"* e recebe **somente as notas novas** (ou `has_changes: false` em milissegundos caso nada tenha mudado).

---

## 1. Estrutura da Planilha com Coluna de Timestamp (Colunas A até P — 16 Colunas)

Sempre que uma nota fiscal entra na planilha, o Google Apps Script grava na **Coluna P (`Timestamp Log`)** a data/hora exata da gravação no formato ISO ordenável (`YYYY-MM-DD HH:MM:SS`, fuso `America/Sao_Paulo`) e atualiza a propriedade interna `LAST_MODIFIED_TS` da planilha.

| Coluna | Índice (`0-15`) | Cabeçalho (Linha 1) | Formato | Exemplo | Campo no Banco |
| :---: | :---: | :--- | :--- | :--- | :--- |
| **A** | `0` | `Data Emissão` | `DD/MM/AAAA HH:MM:SS` | `03/11/2026 14:22:10` | `data_emissao` |
| **B** | `1` | `Competência` | `MM/AAAA` | `11/2026` | `competencia` |
| **C** | `2` | `Nº NF-e` | Texto / Número | `18452` | `numero_nfe` |
| **D** | `3` | `Série` | Texto / Número | `1` | `serie_nfe` |
| **E** | `4` | `Status` | Texto | `Autorizada` | `status` |
| **F** | `5` | `Pedido ML` | Texto numérico | `2000009812345678` | `pedido_ml` |
| **G** | `6` | `Destinatário / Cliente` | Texto | `JOAO DA SILVA` | `cliente_nome` |
| **H** | `7` | `CPF / CNPJ` | Texto numérico | `12345678901` | `cliente_doc` |
| **I** | `8` | `Cidade / UF` | `Cidade - UF` | `São Paulo - SP` | `cidade_uf` |
| **J** | `9` | `Logística` | Texto | `fulfillment` | `logistica` |
| **K** | `10` | `Valor Produtos (R$)` | Numérico (`R$ #,##0.00`) | `149.90` | `valor_produtos` |
| **L** | `11` | `Frete (R$)` | Numérico (`R$ #,##0.00`) | `18.50` | `frete` |
| **M** | `12` | `Valor Total NF-e (R$)` | Numérico (`R$ #,##0.00`) | `168.40` | `valor_total` |
| **N** | `13` | `Itens do Pedido` | Texto | `Sacola Kraft M (2x)` | `itens` |
| **O** | `14` | `Chave de Acesso (44 dígitos)` | Texto (44 dígitos) | `352611...` | `chave_nfe` (**UNIQUE**) |
| **P** | `15` | `Timestamp Log` | `YYYY-MM-DD HH:MM:SS` | `2026-11-03 16:15:00` | `timestamp_log` (**CURSOR**) |

---

## 2. Como Funciona a Leitura por Timestamp (Delta / Log de Alteração)

### Passo Único na Planilha (para ativar a Coluna P e o filtro `since_ts` no Apps Script)
1. Na planilha `notas_ml`, abra **Extensões ➔ Apps Script**.
2. Substitua o código pelo conteúdo atualizado de `c:\dev\ML API\google_apps_script_planilha.gs` (ou copie pelo botão **📊 Google Sheets ➔ 📋 Copiar Código Apps Script** no Dashboard).
3. Clique em **Salvar (💾)** e depois em **Implantar ➔ Gerenciar implantações ➔ Editar (ícone de lápis ✏️) ➔ Versão: "Nova versão" ➔ Implantar**.
   *(Ao editar a implantação existente, a URL continua exatamente a mesma!)*

---

## 3. Endpoints de Consulta Incremental (Somente Leitura)

URL Base do seu Webhook:
```text
https://script.google.com/macros/s/AKfycbxqYYYGWO8oIy9GULK5EvMH6GIeNjIQLw8NEtflS0gm__rmTTXNwhPeVtJtjGS59eifRA/exec
```

### A) Consultar apenas o que mudou após um Timestamp (`since_ts`)
```text
GET .../exec?action=changes&since_ts=2026-11-03 16:00:00
```
- **Se nada mudou desde `2026-11-03 16:00:00`**, o Apps Script nem lê as linhas da planilha — ele responde instantaneamente:
  ```json
  {
    "success": true,
    "has_changes": false,
    "last_ts": "2026-11-03 16:00:00",
    "last_row": 15,
    "total_notes_in_sheet": 14,
    "count": 0,
    "invoices": []
  }
  ```
- **Se entraram novas notas depois de `2026-11-03 16:00:00`**, ele retorna **somente as linhas novas** e o novo `last_ts` para você salvar como seu próximo cursor:
  ```json
  {
    "success": true,
    "has_changes": true,
    "last_ts": "2026-11-03 16:30:12",
    "last_row": 17,
    "total_notes_in_sheet": 16,
    "count": 2,
    "invoices": [
      {
        "row_number": 16,
        "timestamp_log": "2026-11-03 16:30:12",
        "data_emissao": "03/11/2026 16:21:04",
        "competencia": "11/2026",
        "numero_nfe": "18460",
        "serie": "1",
        "status": "Autorizada",
        "order_id": "2000009899991111",
        "buyer_name": "MARIA OLIVEIRA",
        "buyer_doc": "98765432100",
        "cidade_uf": "Curitiba - PR",
        "logistic_type": "fulfillment",
        "valor_produtos": 120.0,
        "frete": 15.9,
        "valor_total": 135.9,
        "itens": "Sacola Personalizada (1x)",
        "chave": "352611..."
      }
    ]
  }
  ```

### B) Consultar apenas linhas novas pelo número da linha (`since_row`)
Como a planilha funciona como um log (*append-only*, novas notas são sempre adicionadas no final), você também pode passar `since_row`:
```text
GET .../exec?action=changes&since_row=15
```
Retorna apenas as linhas da linha `16` em diante!

---

## 4. Código Pronto (Python) para o Outro Aplicativo Consumir em "Tempo Real"

Copie este módulo para o seu outro aplicativo. Ele guarda um arquivo leve de cursor (`cursor_planilha_ml.json`) com o último `last_ts` lido e busca **somente o delta (novas notas)**:

```python
import json
import time
from pathlib import Path
import requests

WEBHOOK_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbxqYYYGWO8oIy9GULK5EvMH6GIeNjIQLw8NEtflS0gm__rmTTXNwhPeVtJtjGS59eifRA/exec"
)
CURSOR_FILE = Path("cursor_planilha_ml.json")


def carregar_cursor() -> dict:
    if CURSOR_FILE.exists():
        try:
            return json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_ts": "", "last_row": 0}


def salvar_cursor(last_ts: str, last_row: int):
    CURSOR_FILE.write_text(
        json.dumps({"last_ts": last_ts, "last_row": last_row}, indent=2),
        encoding="utf-8",
    )


def buscar_novas_notas_delta() -> list:
    """
    Consulta a planilha como um Log de Alterações usando o último Timestamp salvo.
    Retorna apenas as notas novas que entraram desde a última leitura.
    """
    cursor = carregar_cursor()
    since_ts = cursor.get("last_ts", "")

    resp = requests.get(
        WEBHOOK_URL,
        params={"action": "changes", "since_ts": since_ts},
        timeout=30,
        allow_redirects=True,
    )
    resp.raise_for_status()
    dados = resp.json()

    if not dados.get("success"):
        raise RuntimeError(f"Erro ao consultar log da planilha: {dados}")

    novas_notas = dados.get("invoices", [])
    novo_ts = dados.get("last_ts") or since_ts
    nova_row = dados.get("last_row") or cursor.get("last_row", 0)

    # Se houve alterações (ou na primeira execução), atualiza o cursor local
    if novas_notas or (novo_ts and novo_ts != since_ts):
        salvar_cursor(novo_ts, nova_row)

    return novas_notas


def salvar_no_meu_banco(notas: list):
    """
    Substitua pela gravação no banco de dados do seu outro aplicativo.
    Use 'inv["chave"]' como UNIQUE KEY para segurança extra contra duplicidade.
    """
    for inv in notas:
        print(
            f"[NOVA NF-e] TS={inv['timestamp_log']} | NF={inv['numero_nfe']} | "
            f"Pedido={inv['order_id']} | Valor=R$ {inv['valor_total']:.2f} | Chave={inv['chave']}"
        )


if __name__ == "__main__":
    print("Iniciando monitoramento incremental da planilha notas_ml...")
    while True:
        try:
            novas = buscar_novas_notas_delta()
            if novas:
                print(f"Encontradas {len(novas)} nova(s) nota(s) na planilha!")
                salvar_no_meu_banco(novas)
            else:
                print("Nenhuma alteração desde o último timestamp.")
        except Exception as e:
            print(f"Erro na consulta: {e}")

        time.sleep(60)  # Consulta leve a cada 60 segundos
```

---

## 5. Alternativa Nativa do Google Sheets: Query SQL Direta (`gviz/tq`) por Timestamp

Se a planilha estiver compartilhada como *"Qualquer pessoa com o link (Leitor)"*, o próprio Google Sheets possui um motor de banco de dados embutido (**Google Visualization API Query Language**) que permite filtrar a **Coluna P (`Timestamp Log`)** via SQL direto na URL:

```text
https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/gviz/tq?tqx=out:csv&tq=SELECT%20*%20WHERE%20P%20%3E%20'2026-11-03%2016:00:00'
```
- Essa URL executa `SELECT * WHERE P > '2026-11-03 16:00:00'` diretamente nos servidores do Google e devolve um CSV contendo **apenas as linhas cujo Timestamp (Coluna P) seja maior que o informado**!
