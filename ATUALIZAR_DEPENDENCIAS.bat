@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 (
  echo Nao consegui entrar na pasta do CRM.
  pause
  exit /b 1
)
echo Atualizando bibliotecas do CRM
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
)

:cria_venv
if exist "%CD%\.venv\Scripts\python.exe" goto :pip
echo Criando ambiente virtual com Python 3.12...
py -3.12 -m venv .venv
if errorlevel 1 (
  echo Falha ao criar o ambiente virtual. Instale o Python 3.12 64 bits.
  pause
  exit /b 1
)

:pip
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Nao foi possivel atualizar as dependencias. Confira se o Python e 3.12.
  pause
  exit /b 1
)
echo Dependencias atualizadas.
pause
exit /b 0

:sem_python
echo Este CRM precisa do Python 3.12 64 bits.
echo Instale e marque o py launcher e Add python.exe to PATH.
pause
exit /b 1
