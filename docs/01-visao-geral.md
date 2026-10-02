# Visão geral e fluxo de dados

O CRM NPD é o painel comercial da NPD Embalagens. Ele **não substitui o Sisplan**:
lê o que o ERP já faturou, pediu, orçou e recebeu, e calcula em cima disso
comissão, score, funil e tarefas de recompra.

```
Sisplan (Firebird 14215F.FDB)     somente SELECT
        │
        │  sync/  ou  CSV (fallback)
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

- **Primária hoje:** SQLite local, alimentado pela importação CSV dos relatórios
  721 (pedidos), 731 (faturamento), 104 (orçamentos) e 733 (liquidez).
- **Em paralelo:** `crm_npd/sync/` lê o Firebird e grava tabelas-espelho
  `erp_*`. Só vira fonte primária depois que `sync.reconcile` casar
  centavo a centavo com o controle (set/2026 = R$ 997.044,89 em 518 linhas).
- **O CRM grava** tarefas, identidades, metas, usuários e overrides. Isso nunca
  volta para o Sisplan.

## Identidade comercial

Um "cliente" no CRM não é uma linha de `customers`. É uma **identidade**:

`GRUPO:…` > `REDE:…` / `MATRIZ:…` > `CLIENTE:…`

Matriz e filiais da mesma rede compartilham histórico, score e comissão de
"cliente novo". A chave está em `customer_identity_rules.identity_key`.

## Ciclo de faturamento

Não é o mês-calendário. Com fechamento no dia 24 (configurável em
`ciclos_faturamento`), setembro/2026 vai de **25/08 a 24/09**. Funções em
`crm_npd.metrics.periodos`.

## O que ainda está no monolito

`crm_npd/legacy_monolith.py` ainda concentra rotas e HTML. As fórmulas já saíram
para `metrics/`. O SQL das telas está começando a sair para
`repositories/sqlite/`. A extração de blueprints e templates continua na Fase 3.
