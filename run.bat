@echo off
cd /d %~dp0

py -3.12 -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo Este CRM precisa do Python 3.12 (64 bits).
  echo Baixe em https://www.python.org/downloads/windows/
  echo Na instalacao marque "py launcher" e "Add python.exe to PATH".
  pause
  exit /b 1
)

if exist ".venv\Scripts\python.exe" (
  .venv\Scripts\python.exe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"
  if errorlevel 1 (
    echo O .venv atual nao e Python 3.12. Recriando...
    rmdir /s /q .venv
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo Ambiente virtual nao encontrado. Criando com Python 3.12...
  py -3.12 -m venv .venv
  if errorlevel 1 (
    echo Falha ao criar o ambiente virtual. Instale o Python 3.12 e tente novamente.
    pause
    exit /b 1
  )
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Falha ao instalar as dependencias. Confira se o Python e 3.12.
    pause
    exit /b 1
  )
)

echo Se a base for de outro dia ou de outro PC, a primeira subida recarrega o Sisplan.
echo Isso pode levar alguns minutos; o ouvinte CDC entra depois.
.venv\Scripts\python.exe app.py
pause
