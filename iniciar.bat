@echo off
echo ============================================
echo   Comparador de Tabela Imobiliaria
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo ERRO: Python nao encontrado.
    echo.
    echo Instale em https://python.org
    echo Na instalacao, marque a opcao "Add Python to PATH"
    echo Depois feche e abra este arquivo novamente.
    echo.
    pause
    exit /b 1
)

echo Instalando dependencias...
python -m pip install -r "%~dp0requirements.txt" --quiet
if errorlevel 1 (
    echo ERRO ao instalar dependencias.
    pause
    exit /b 1
)
echo.

echo Verificando navegador do bot...
python -m playwright install chromium --quiet
echo.

echo Iniciando app...
echo (Uma aba vai abrir no seu navegador automaticamente)
echo.
python -m streamlit run "%~dp0app.py"

pause
