@echo off
cd /d %~dp0
echo Atualizando bibliotecas do CRM no ambiente virtual...

if not exist ".venv\Scripts\python.exe" (
  echo Criando ambiente virtual...
  py -m venv .venv
  if errorlevel 1 (
    echo Falha ao criar o ambiente virtual. Instale o Python 3.12.
    pause
    exit /b 1
  )
)

.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Nao foi possivel atualizar as dependencias. Verifique a internet e o Python.
  pause
  exit /b 1
)
echo.
echo Dependencias atualizadas. A IA OpenAI e o CRM estao prontos para uso.
pause
