@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 (
  echo Nao consegui entrar na pasta do CRM.
  pause
  exit /b 1
)
echo Pasta: %CD%

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
REM Automacao Mercado Livre (Google Sheets notas_ml):
REM 1. Verifica as notas assim que o sistema inicia (ML_SHEETS_SYNC_ON_START=1)
REM 2. Mantem verificacao automatica por Timestamp Log a cada 60 segundos
if not defined ML_SHEETS_SYNC_ON_START set ML_SHEETS_SYNC_ON_START=1
if not defined ML_SHEETS_SYNC_AUTO set ML_SHEETS_SYNC_AUTO=1
if not defined ML_SHEETS_SYNC_INTERVAL set ML_SHEETS_SYNC_INTERVAL=60

echo A primeira subida le o Sisplan so em SELECT e atualiza o CRM. Nada e gravado no ERP.
echo Mercado Livre (Google Sheets): verifica notas no inicio e monitora Timestamp Log a cada %ML_SHEETS_SYNC_INTERVAL%s.
echo Pode levar alguns minutos. Os ouvintes CDC entram depois.
".venv\Scripts\python.exe" app.py
echo.
pause
exit /b %ERRORLEVEL%

:sem_python
echo Este CRM precisa do Python 3.12 64 bits.
echo Instale e marque o py launcher e Add python.exe to PATH.
pause
exit /b 1
