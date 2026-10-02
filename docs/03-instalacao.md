# Guia de instalação

## Requisitos

- Windows 10/11
- Python 3.12 ou 3.13 (64 bits)
- Este repositório

Firebird **não** é necessário para usar o CRM no dia a dia. Só entra quando
formos ler o Sisplan direto (Fase 4/5).

## Primeira subida

```bat
cd "D:\dev npd\CRM NPD"
ATUALIZAR_DEPENDENCIAS.bat
copy .env.example .env
```

Edite `.env` e troque `CRM_NPD_SECRET`. Não commite esse arquivo.

```bat
run.bat
```

O script cria `.venv` se faltar e sobe o Flask com o Python do venv. Abra
`http://127.0.0.1:5000`.

## Diagnóstico

```bat
.venv\Scripts\python.exe -m crm_npd.cli.doctor
```

Confere venv, Flask, `.env`, banco SQLite e (opcional) cliente Firebird.

## Testes

```bat
.venv\Scripts\python.exe -m pytest tests/unit -q
.venv\Scripts\python.exe -m pytest tests/golden -q
```

O golden master leva ~3 minutos: recaptura todos os números da base congelada
em `tests/golden/fixture/baseline.db` e compara com
`tests/golden/baseline/golden.json`.

## Credenciais do Sisplan

Ficam só no `.env` (`SISPLAN_*`). O manual
`MANUAL_ACESSO_BANCO_SISPLAN.md` descreve host, porta e a **regra de ouro**:
nenhuma conexão com `14215F.FDB` pode emitir outra coisa além de `SELECT`.

Nesta máquina a cópia `14215F.FDB` na raiz do projeto abre em modo **Embedded**
(`SISPLAN_HOST=local`), sem o servidor da fábrica. O cliente fica em
`vendor/firebird/fbclient.dll` (não versionado). Prova de leitura:

```
.venv\Scripts\python.exe scripts\probe_firebird_local.py
```

## Atualizar dependências

```bat
ATUALIZAR_DEPENDENCIAS.bat
```

Versões estão pinadas em `requirements.txt` e `requirements-dev.txt`.
