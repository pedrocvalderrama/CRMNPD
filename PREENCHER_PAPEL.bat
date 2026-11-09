@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Ambiente virtual nao encontrado. Rode ATUALIZAR_DEPENDENCIAS.bat primeiro.
  pause
  exit /b 1
)

echo ====================================================================
echo   PREENCHIMENTO DO TIPO DE PAPEL NO FATURAMENTO
echo ====================================================================
echo.
echo Atualizando o faturamento completo do CRM a partir do Sisplan.
echo O Tipo de Papel sera preenchido pela coluna DESC_MARCA.
echo Esta rotina recarrega o faturamento, incluindo valores, frete e cliches.
echo Pedidos e demais modulos nao serao recarregados.
echo O banco do Sisplan sera acessado somente para leitura.
echo Aguarde. A operacao pode levar alguns minutos.
echo.

".venv\Scripts\python.exe" -m crm_npd.cli.sync_sisplan --faturamento
if errorlevel 1 (
  echo.
  echo [ERRO] Falha ao atualizar o faturamento.
  echo Verifique a conexao com o Firebird e as configuracoes SISPLAN no .env.
  pause
  exit /b 1
)

echo.
echo Tipo de Papel preenchido no faturamento com sucesso!
echo Atualize a tela de Desempenho para consultar os filtros.
echo.
pause
exit /b 0
