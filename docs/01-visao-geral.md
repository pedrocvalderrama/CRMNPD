# Visão geral e fluxo de dados

O CRM NPD é o painel comercial da NPD Embalagens. Ele **não substitui o Sisplan**:
lê o que o ERP já faturou, pediu, orçou e recebeu, e calcula em cima disso
comissão, score, funil e tarefas de recompra.

```
Sisplan (Firebird 14215F.FDB)     somente SELECT
        │
        │  sync/ (caminho do FDB no .env)
        ▼
SQLite crm_npd.db                 banco do CRM + espelho
        │
        ├── repositories/sqlite/  SQL
        ├── services/             orquestração
        ├── metrics/              fórmulas puras
        └── web/ + monolito       telas Flask
```

## Camadas

| Pasta | Pode falar com | Não pode |
|---|---|---|
| `domain/` | nada de infra | sqlite3, flask, fdb |
| `metrics/` | `domain/` | sqlite3, flask, fdb |
| `repositories/sqlite/` | SQLite | Firebird, Flask |
| `repositories/firebird/` | Firebird, só SELECT | SQLite, Flask |
| `services/` | repositórios e métricas | HTML |
| `web/` e `legacy_monolith.py` | services e Flask | fdb |
| `sync/` | os dois repositórios | HTML |

A regra é testada em `tests/unit/test_arquitetura.py`.

## Fontes de dados

- **Primária:** o Firebird do Sisplan (`14215F.FDB`), só SELECT.
  Na fábrica o CRM abre `192.168.100.168:3051` → `C:\Sisplan\Base\14215F.FDB`
  (ver `MANUAL_ACESSO_BANCO_SISPLAN.md`). Desenvolvimento local pode usar
  `SISPLAN_HOST=local` e a cópia do FDB na pasta `base\`.
- **Espelho:** o sync (carga completa) e o ouvinte CDC (`LOG` /
  `LOG_ALTERACAO`) gravam pedidos, faturamento, orçamentos e liquidez no
  SQLite do CRM. CSV não entra mais.
- **O CRM grava** tarefas, identidades, metas, usuários e overrides. Isso nunca
  volta para o Sisplan.

## Identidade comercial

Um "cliente" no CRM não é uma linha de `customers`. É uma **identidade**:

`GRUPO:…` > `REDE:…` / `MATRIZ:…` > `CLIENTE:…`

Matriz e filiais da mesma rede compartilham histórico, score e comissão de
"cliente novo". A chave está em `customer_identity_rules.identity_key`.

## Ciclo de faturamento

Não é o mês-calendário. Com fechamento no dia 25 (configurável em
`ciclos_faturamento`), setembro/2026 vai de **26/08 a 25/09**. Funções em
`crm_npd.metrics.periodos`.

## O que ainda está no monolito

`crm_npd/legacy_monolith.py` ainda concentra rotas e HTML. As fórmulas já saíram
para `metrics/`. O SQL das telas está começando a sair para
`repositories/sqlite/`. A extração de blueprints e templates continua na Fase 3.
