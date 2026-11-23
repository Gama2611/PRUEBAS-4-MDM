@echo off
chcp 65001 >nul
title ADIPACK MDM - Crear base de datos PostgreSQL
cd /d "%~dp0server"
where python >nul 2>nul
if errorlevel 1 (
  echo No se encontro Python. Instala Python 3.11 o superior desde python.org marcando "Add python.exe to PATH".
  pause
  exit /b 1
)
if not exist "..\venv\Scripts\python.exe" (
  echo Preparando el entorno por primera vez. Necesita Internet...
  python -m venv ..\venv
  if errorlevel 1 goto error
  "..\venv\Scripts\python.exe" -m pip install --upgrade pip
  "..\venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto error
)
"..\venv\Scripts\python.exe" crear_base_datos.py %*
pause
exit /b 0
:error
echo ERROR preparando el entorno.
rmdir /s /q "..\venv"
pause
exit /b 1
