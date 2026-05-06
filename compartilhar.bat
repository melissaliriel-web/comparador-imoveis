@echo off
title Comparador - Link Publico
echo ============================================
echo   Comparador de Tabela Imobiliaria
echo   Gerando link para acesso externo...
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo ERRO: Python nao encontrado.
    echo Execute primeiro o iniciar.bat para instalar tudo.
    pause
    exit /b 1
)

echo [1/3] Instalando dependencias se necessario...
python -m pip install -r "%~dp0requirements.txt" --quiet
python -m playwright install chromium --quiet
echo.

echo [2/3] Iniciando o app em segundo plano...
start "ComparadorApp" /b python -m streamlit run "%~dp0app.py" --server.headless true --server.port 8501 --server.address localhost
timeout /t 5 >nul
echo.

echo [3/3] Criando link publico...
echo.
echo ================================================================
echo  AGUARDE — o link vai aparecer em alguns segundos...
echo  Copie o link "https://..." e envie para o PC do trabalho.
echo.
echo  IMPORTANTE: deixe esta janela ABERTA enquanto estiver usando.
echo  Fechar esta janela desconecta o link.
echo ================================================================
echo.

ssh -o StrictHostKeyChecking=no -R 80:localhost:8501 nokey@localhost.run

echo.
echo Link encerrado.
pause
