@echo off
chcp 65001 >nul
title ADIPACK MDM
cd /d "%~dp0server"
where python >nul 2>nul
if errorlevel 1 (
  echo No se encontro Python. Instala Python 3.11 o superior desde python.org marcando "Add python.exe to PATH".
  pause
  exit /b 1
)
if not exist "..\venv\Scripts\python.exe" (
  echo Primera ejecucion: preparando el entorno. Necesita Internet y tarda 2-5 minutos...
  python -m venv ..\venv
  if errorlevel 1 goto error
  "..\venv\Scripts\python.exe" -m pip install --upgrade pip
  "..\venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto error
  copy /y requirements.txt "..\venv\requirements.instalado" >nul
)
rem Al actualizar el MDM (nueva carpeta server) se instalan solos los componentes nuevos.
fc /b requirements.txt "..\venv\requirements.instalado" >nul 2>nul
if errorlevel 1 (
  echo Actualizando componentes del MDM. Necesita Internet, 1-2 minutos...
  "..\venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto error_upd
  copy /y requirements.txt "..\venv\requirements.instalado" >nul
)
"..\venv\Scripts\python.exe" iniciar_mdm.py
pause
exit /b 0
:error
echo ERROR preparando el entorno. Revisa tu conexion a Internet y vuelve a intentar.
rmdir /s /q "..\venv"
pause
exit /b 1
:error_upd
echo ERROR instalando los componentes nuevos. Revisa tu conexion a Internet y vuelve a ejecutar INICIAR_MDM.bat.
pause
exit /b 1
