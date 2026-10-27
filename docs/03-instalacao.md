# Guia de instalação

## Requisitos

- Windows 10/11
- Python **3.12** ou 3.13 (64 bits). Python 3.10 **não** serve
  (`matplotlib` e `numpy` desta versão exigem ≥ 3.11). No Windows use o
  instalador oficial e marque o *py launcher*.
- Este repositório

O CRM lê o Sisplan no servidor da fábrica (`192.168.100.168:3051`,
arquivo `C:\Sisplan\Base\14215F.FDB`), conforme
`MANUAL_ACESSO_BANCO_SISPLAN.md`. Nada além de SELECT. O caminho do FDB
é o caminho **no servidor Firebird**, mesmo se o CRM rodar em outro PC.

Para desenvolver nesta máquina sem a rede da fábrica, use
`SISPLAN_HOST=local` e a cópia `14215F.FDB` na pasta `base\`.

## Primeira subida

```bat
cd "D:\dev npd\CRM NPD"
ATUALIZAR_DEPENDENCIAS.bat
copy .env.example .env
```

Edite `.env`: troque `CRM_NPD_SECRET` e preencha `SISPLAN_PASSWORD`.
Não commite esse arquivo.

No servidor da fábrica deixe `SISPLAN_HOST=192.168.100.168` (não use
`local`). `SISPLAN_CDC_AUTO=1` sobe o ouvinte dos logs junto com o Flask.

A base copiada deste PC é de ontem: o ouvinte **não** reconstitui o dia
que faltou. Na primeira subida do Flask no servidor (`SISPLAN_SYNC_ON_START=stale`)
o CRM relê o FDB inteiro e só então liga o ouvinte. Pode levar alguns
minutos. Reinícios no mesmo dia pulam a carga.

Para forçar na mão:

```bat
ATUALIZAR_BASE_SISPLAN.bat
run.bat
```

O script cria `.venv` se faltar e sobe o Flask com o Python do venv. Abra
`http://127.0.0.1:8089`.

## Testar nesta máquina (só SQLite)

O `run.bat` do servidor continua falando com o Sisplan. Para abrir o CRM
aqui e mexer em tela, **não** use o `run.bat`.

1. No servidor, pare o Flask.
2. Copie `crm_npd.db` (e, se existirem, `crm_npd.db-wal` e
   `crm_npd.db-shm`) da pasta do CRM no servidor para esta pasta.
3. Aqui:

```bat
run_local.bat
```

Abra `http://127.0.0.1:8089`. Essa janela força `CRM_NPD_FONTE=sqlite`,
desliga o ouvinte e a carga do FDB. O Sisplan não é aberto.

O `.env` está no `.gitignore`. Commit de código não leva host, senha nem
fonte do banco. No servidor continue usando o `.env` de lá e o `run.bat`.

O ouvinte CDC (pedidos, faturamento, orçamentos) sobe na mesma janela se
`SISPLAN_CDC_AUTO=1`. Para rodá-lo separado:

```bat
OUVINTE_SISPLAN.bat
```

## Diagnóstico

```bat
.venv\Scripts\python.exe -m crm_npd.cli.doctor
```

Confere venv, Flask, `.env`, banco SQLite e a conexão somente leitura com o Sisplan.

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
`MANUAL_ACESSO_BANCO_SISPLAN.md` descreve host `192.168.100.168`, porta
`3051`, banco `C:\Sisplan\Base\14215F.FDB` e a **regra de ouro**:
nenhuma conexão com o Sisplan pode emitir outra coisa além de `SELECT`.

O cliente Firebird sai de `vendor/firebird/fbclient.dll` (junto do projeto)
ou do Firebird já instalado no Windows. Não é preciso `C:\dev\auto_sisplan`.
Prova de leitura na fábrica:

```
.venv\Scripts\python.exe scripts\probe_firebird.py
```

Cópia local (máquina do programador):

```
.venv\Scripts\python.exe scripts\probe_firebird_local.py
```

## Atualizar dependências

```bat
ATUALIZAR_DEPENDENCIAS.bat
```

Versões estão pinadas em `requirements.txt` e `requirements-dev.txt`.
