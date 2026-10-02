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

```bat
.venv\Scripts\python.exe -c "from crm_npd.sync.etl import sincronizar_periodo; ..."
```

Só funciona com `SISPLAN_PASSWORD` no `.env`, `fbclient_x64.dll` e o servidor
`192.168.100.168:3051` alcançável. Enquanto isso não existir nesta máquina, o
CSV continua sendo a fonte.

O cutover (`CRM_NPD_FONTE=firebird`) só depois de
`reconciliar_controle_setembro` devolver `ok: true`.

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
