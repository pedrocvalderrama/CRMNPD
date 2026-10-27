@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 (
  echo Nao consegui entrar na pasta do CRM.
  pause
  exit /b 1
)
echo Pasta: %CD%

REM Trava desta janela: so SQLite. Nao abre o Firebird, nao sobe o ouvinte.
REM Estas variaveis ganham do .env. O run.bat do servidor continua no padrao da fabrica.
set CRM_NPD_FONTE=sqlite
set SISPLAN_CDC_AUTO=0
set SISPLAN_SYNC_ON_START=0
set CRM_NPD_HOST=127.0.0.1

if exist "%CD%\crm_npd.db" goto :tem_base
echo.
echo Falta crm_npd.db nesta pasta.
echo Copie do servidor a pasta do CRM: crm_npd.db
echo Se existirem crm_npd.db-wal e crm_npd.db-shm, copie os tres juntos.
echo O Flask no servidor precisa estar parado na hora da copia, para o arquivo ficar consistente.
echo.
pause
exit /b 1

:tem_base
py -3.12 -c "import sys" >nul 2>&1
if errorlevel 1 goto :sem_python

if exist "%CD%\.venv\Scripts\python.exe" goto :checa_venv
goto :cria_venv

:checa_venv
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"
if errorlevel 1 (
  echo O .venv atual nao e Python 3.12. Recriando...
  rmdir /s /q .venv
  goto :cria_venv
)
goto :sobe

:cria_venv
echo Ambiente virtual nao encontrado. Criando com Python 3.12...
py -3.12 -m venv .venv
if errorlevel 1 (
  echo Falha ao criar o ambiente virtual. Instale o Python 3.12 e tente novamente.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Falha ao instalar as dependencias. Confira se o Python e 3.12.
  pause
  exit /b 1
)

:sobe
echo.
echo Modo local: somente SQLite. O Sisplan nao sera aberto.
echo Abra http://127.0.0.1:8089
echo Para o servidor da fabrica continue usando run.bat.
echo.
".venv\Scripts\python.exe" app.py
echo.
pause
exit /b %ERRORLEVEL%

:sem_python
echo Este CRM precisa do Python 3.12 64 bits.
echo Instale e marque o py launcher e Add python.exe to PATH.
pause
exit /b 1
