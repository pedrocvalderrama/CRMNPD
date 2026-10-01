@echo off
cd /d %~dp0
echo Atualizando bibliotecas do CRM...
py -m pip install --upgrade -r requirements.txt
if errorlevel 1 (
  echo.
  echo Nao foi possivel atualizar as dependencias. Verifique a internet e o Python.
  pause
  exit /b 1
)
echo.
echo Dependencias atualizadas. A IA OpenAI e o CRM estao prontos para uso.
pause
