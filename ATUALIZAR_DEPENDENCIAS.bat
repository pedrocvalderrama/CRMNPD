@echo off
cd /d %~dp0
echo Atualizando bibliotecas do CRM no ambiente virtual...

py -3.12 -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo.
  echo Este CRM precisa do Python 3.12 (64 bits). O que esta instalado nao serve.
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
  echo Criando ambiente virtual com Python 3.12...
  py -3.12 -m venv .venv
  if errorlevel 1 (
    echo Falha ao criar o ambiente virtual. Instale o Python 3.12 (64 bits).
    pause
    exit /b 1
  )
)

.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Nao foi possivel atualizar as dependencias.
  echo Confira se o Python e 3.12 (nao 3.10) e se ha internet.
  pause
  exit /b 1
)
echo.
echo Dependencias atualizadas. A IA OpenAI e o CRM estao prontos para uso.
pause
