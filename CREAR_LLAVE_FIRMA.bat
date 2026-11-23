@echo off
chcp 65001 >nul
title ADIPACK MDM - Crear llave de firma
rem Crea UNA SOLA VEZ la llave para firmar las apps Android y los 4 Secrets para GitHub.
rem La llave queda en Documentos\ADIPACK_MDM_LLAVE (fuera del proyecto: nunca se sube a GitHub).
cd /d "%~dp0"
set PY=
if exist "venv\Scripts\python.exe" set PY=venv\Scripts\python.exe
if not defined PY (
  where python >nul 2>nul
  if errorlevel 1 goto sinpython
  set PY=python
)
"%PY%" tools\crear_llave_firma.py
if errorlevel 1 (
  echo.
  echo ERROR al crear la llave. Revisa la conexion a Internet, la necesita la primera vez.
)
pause
exit /b 0
:sinpython
echo No se encontro Python. Ejecuta primero INICIAR_MDM.bat o instala Python 3.11 o superior.
pause
exit /b 1
