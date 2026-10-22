# Guia de manutenção e extensão

## Antes de mudar um número

1. Rode `pytest tests/unit tests/golden`.
2. Se o golden falhar e a mudança **não** foi aprovada, é regressão: corrija o
   código, não o baseline.
3. Se a mudança foi aprovada (como as unificações de 2026-10-01), regrave:

```bat
.venv\Scripts\python.exe scripts/golden_capture.py --baseline
```

Descreva no commit **qual regra mudou e por quê**. Nunca regrave o baseline
só para o teste passar.

## Onde colocar código novo

| Quero… | Coloco em… |
|---|---|
| Uma fórmula (porcentagem, score, comissão) | `crm_npd/metrics/` + teste em `tests/unit/` |
| Um tipo de negócio | `crm_npd/domain/` |
| Uma consulta SQL no CRM | `crm_npd/repositories/sqlite/` |
| Uma consulta no Sisplan | `crm_npd/repositories/firebird/` (SELECT) |
| Orquestrar fórmula + dado | `crm_npd/services/` |
| Uma tela nova | blueprint em `crm_npd/web/` + template em `templates/` |
| Uma coluna nova no SQLite | nova versão em `aplicar_migracoes`, nunca `ALTER` solto |

É proibido abrir `sqlite3` ou `fdb` dentro de `metrics/` ou `domain/`. O teste
de arquitetura barra isso.

## Migracoes

`crm_npd/migrations.py` agora registra versões em `schema_migrations`. Bases
já existentes são carimbadas, sem reexecutar o schema. A próxima mudança de
schema ganha a versão 3: uma função nova + `_carimbar(conn, 3, "nome")`.

`init_db` do monolito ainda cria as tabelas-base (versão 1). Não apague essa
função até a extração da Fase 3 terminar.

## Sync Firebird

Carga completa (primeira vez no servidor, base de ontem, ou se o ouvinte
ficou parado muito tempo). O `run.bat` já faz isso sozinho se o snapshot
não for de hoje. Na mão:

```bat
ATUALIZAR_BASE_SISPLAN.bat
```

Ou o botão **Ler FDB agora** em Atualizar Sisplan. Substitui pedidos,
faturamento, orçamentos e liquidez. A conexão padrão é a da fábrica
(`192.168.100.168:3051` / `C:\Sisplan\Base\14215F.FDB`).

## Ouvinte CDC (tempo quase real)

`MANUAL_CDC_LOGS_SISPLAN.md`. A cada 5 segundos o CRM faz SELECT em
`LOG_ALTERACAO` e `LOG` (`ID > cursor`). Se o log for de pedido, nota ou
orçamento, regrava só aquelas chaves no SQLite. Cold start grava o
`MAX(ID)` atual e **não** reprocessa o histórico — a carga completa é o
`sync_sisplan`.

```bat
OUVINTE_SISPLAN.bat
```

Com `SISPLAN_CDC_AUTO=1` no `.env`, o Flask já sobe a thread. Não rode o
`.bat` ao mesmo tempo, senão dois processos avançam o mesmo cursor.

O cursor fica em `settings` (`cdc_last_log_alteracao_id`,
`cdc_last_log_id`). Se o gravar no CRM falhar, o cursor não anda e o
próximo ciclo tenta de novo.

## Extração do monolito

`legacy_monolith.py` ainda é o app. Extraia nesta ordem, uma rota por vez:

1. Mover o SQL da view para um repositório.
2. Mover o HTML para `templates/`.
3. Trocar `render_template_string` por `render_template`.
4. Registrar a rota num blueprint e apagar a função do monolito.
5. Rodar o golden.

`patches.py` substitui funções em runtime. Se a tela não bate com o código que
você está lendo, olhe o patch.

## Segredos

Chaves de API (Gemini, etc.) que existirem na tabela `settings` devem sair de
lá e ir para o `.env`. O banco não entra no git, mas backup de `.db` vaza
segredo do mesmo jeito.
