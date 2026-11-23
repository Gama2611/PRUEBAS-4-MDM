@echo off
chcp 65001 >nul
title ADIPACK MDM - Crear acceso directo
rem Crea en el escritorio el acceso directo "ADIPACK MDM" con el logo, que abre INICIAR_MDM.bat.
rem Se puede anclar a la barra de tareas o al menu Inicio (clic derecho sobre el acceso directo).
if not exist "%~dp0ADIPACK_MDM.ico" (
  echo No se encontro ADIPACK_MDM.ico junto a este archivo.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'ADIPACK MDM.lnk')); $s.TargetPath='%~dp0INICIAR_MDM.bat'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0ADIPACK_MDM.ico,0'; $s.Description='Servidor ADIPACK MDM'; $s.Save()"
if errorlevel 1 (
  echo ERROR: no se pudo crear el acceso directo.
) else (
  echo LISTO: en el escritorio esta el acceso directo "ADIPACK MDM" con el logo.
)
pause
