@echo off
chcp 65001 >nul
title ADIPACK MDM - Abrir Firewall
rem Abre en el Firewall de Windows el puerto del MDM para que las tablets descarguen el agente.
rem Pide permiso de administrador una sola vez.
net session >nul 2>&1
if errorlevel 1 (
  echo Pidiendo permiso de administrador...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b 0
)
cd /d "%~dp0server"
set PORT=8000
if exist ".env" (
  for /f "tokens=1,* delims==" %%a in ('findstr /b /c:"MDM_PORT=" ".env"') do set PORT=%%b
)
set PORT=%PORT: =%
set MQTTPORT=1883
if exist ".env" (
  for /f "tokens=1,* delims==" %%a in ('findstr /b /c:"MDM_MQTT_PORT=" ".env"') do set MQTTPORT=%%b
)
set MQTTPORT=%MQTTPORT: =%
rem Si alguna vez se pulso "Cancelar" en el aviso del Firewall, Windows creo reglas que BLOQUEAN a Python
rem (y un bloqueo gana sobre cualquier permiso). Se borran y se crea un permiso para el Python del MDM.
set PYEXE=
if not exist ..\venv\Scripts\python.exe goto puerto
..\venv\Scripts\python.exe -c "import os,sys;open(os.path.join(os.environ['TEMP'],'adipack_pyexe.txt'),'w').write(sys._base_executable)"
if exist "%TEMP%\adipack_pyexe.txt" set /p PYEXE=<"%TEMP%\adipack_pyexe.txt"
del "%TEMP%\adipack_pyexe.txt" >nul 2>&1
if not defined PYEXE goto puerto
echo Revisando reglas de Python: "%PYEXE%"
netsh advfirewall firewall delete rule name=all dir=in program="%PYEXE%" >nul 2>&1
netsh advfirewall firewall add rule name="ADIPACK MDM Python" dir=in action=allow program="%PYEXE%" profile=any >nul
:puerto
echo Abriendo el puerto TCP %PORT% para todas las redes (dominio, privada y publica)...
netsh advfirewall firewall delete rule name="ADIPACK MDM %PORT%" >nul 2>&1
netsh advfirewall firewall add rule name="ADIPACK MDM %PORT%" dir=in action=allow protocol=TCP localport=%PORT% profile=any
if errorlevel 1 goto fallo
echo Abriendo el puerto TCP %MQTTPORT% de los avisos MQTT...
netsh advfirewall firewall delete rule name="ADIPACK MDM %MQTTPORT%" >nul 2>&1
netsh advfirewall firewall add rule name="ADIPACK MDM %MQTTPORT%" dir=in action=allow protocol=TCP localport=%MQTTPORT% profile=any
:fallo
if errorlevel 1 (
  echo ERROR: no se pudo crear la regla. Pide ayuda a TI: puede haber un antivirus o una politica que lo bloquee.
) else (
  echo.
  echo LISTO. Puertos abiertos: %PORT% para la consola y el QR, %MQTTPORT% para los avisos MQTT.
)
pause
