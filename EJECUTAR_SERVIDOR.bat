@echo off
chcp 65001 >nul
title Servidor Local Historias Clinicas Proinsalud v5.4.0
cls

echo ========================================================================
echo Iniciando Servidor Local Historias Clinicas Proinsalud v5.4.0...
echo ========================================================================

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python no se encuentra instalado o no esta agregado al PATH system.
    echo Instale Python 3.8+ y reintente.
    pause
    exit /b 1
)

python "%~dp0server.py" %*

if %errorlevel% neq 0 (
    echo.
    echo El servidor finalizo con algun error.
    pause
)
