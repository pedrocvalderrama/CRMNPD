@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Ambiente virtual não encontrado em .venv.
  pause
  exit /b 1
)

echo ====================================================================
echo   PREENCHIMENTO DO HISTÓRICO DE FATURAMENTO (FRETE E CLICHÊS)
echo ====================================================================
echo.
echo Lendo dados do Sisplan (Firebird) e atualizando o histórico completo
echo de faturamento no CRM com frete e clichês...
echo.
.venv\Scripts\python.exe -m crm_npd.cli.sync_sisplan --faturamento
if errorlevel 1 (
  echo.
  echo [ERRO] Falha na carga. Verifique se o serviço do Firebird está ativo
  echo e se as credenciais do .env estão acessíveis.
  echo.
  pause
  exit /b 1
)

echo.
echo ====================================================================
echo   Histórico de faturamento preenchido com sucesso!
echo ====================================================================
echo.
pause
