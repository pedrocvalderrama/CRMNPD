@echo off
cd /d %~dp0

if not exist ".venv\Scripts\python.exe" (
  echo Ambiente virtual nao encontrado. Rode ATUALIZAR_DEPENDENCIAS.bat primeiro.
  pause
  exit /b 1
)

echo Carga completa do Sisplan (pedidos, faturamento, orcamentos, liquidez).
echo Somente SELECT. Pode levar alguns minutos.
.venv\Scripts\python.exe -m crm_npd.cli.sync_sisplan
if errorlevel 1 (
  echo Falha na carga. Confira SISPLAN_PASSWORD e o host no .env.
  pause
  exit /b 1
)
pause
