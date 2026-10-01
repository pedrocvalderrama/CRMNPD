@echo off
cd /d %~dp0

if not exist ".venv\Scripts\python.exe" (
  echo Ambiente virtual nao encontrado. Criando...
  py -m venv .venv
  if errorlevel 1 (
    echo Falha ao criar o ambiente virtual. Instale o Python 3.12 e tente novamente.
    pause
    exit /b 1
  )
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Falha ao instalar as dependencias. Verifique a internet.
    pause
    exit /b 1
  )
)

.venv\Scripts\python.exe app.py
pause
