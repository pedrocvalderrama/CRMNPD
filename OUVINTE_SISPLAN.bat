@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Ambiente virtual nao encontrado. Rode ATUALIZAR_DEPENDENCIAS.bat primeiro.
  pause
  exit /b 1
)

echo Ouvinte CDC do Sisplan — pedidos, faturamento e orcamentos.
echo Somente SELECT. Ctrl+C para encerrar.
.venv\Scripts\python.exe -m crm_npd.cli.cdc
pause
