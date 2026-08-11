@echo off
chcp 65001 >nul
title Instalador de Dependencias Opcionales
cls

echo ========================================================================
echo Instalando dependencias opcionales de Playwright para raspado avanzado...
echo ========================================================================

python -m pip install --upgrade pip
python -m pip install playwright beautifulsoup4 requests

echo.
echo Instalando navegadores de Playwright...
python -m playwright install chromium

echo.
echo ========================================================================
echo ¡Instalacion completada!
echo ========================================================================
pause
