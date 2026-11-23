# Ejecutar como Administrador:  powershell -ExecutionPolicy Bypass -File install_windows.ps1 -Server http://192.168.1.10:8000 -Token TOKEN
param([Parameter(Mandatory)][string]$Server, [Parameter(Mandatory)][string]$Token)
$ErrorActionPreference = "Stop"
$dir = "C:\Program Files\AdipackMDM"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Copy-Item "$PSScriptRoot\agent.py" $dir -Force
$py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { throw "Instala Python 3.10+ (con 'Add to PATH') antes de continuar" }
& ($py -replace 'pythonw.exe','python.exe') -m pip install --quiet psutil
& ($py -replace 'pythonw.exe','python.exe') "$dir\agent.py" enroll --server $Server --token $Token
# Permitir que el usuario de planta lea/escriba la config del agente
icacls "$env:ProgramData\AdipackMDM" /grant "*S-1-5-32-545:(OI)(CI)M" | Out-Null
# Tarea al iniciar sesión de CUALQUIER usuario (necesario para abrir el kiosco en su escritorio)
$action  = New-ScheduledTaskAction -Execute $py -Argument "`"$dir\agent.py`" run"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$princ   = New-ScheduledTaskPrincipal -GroupId "S-1-5-32-545" -RunLevel Limited
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0)
Register-ScheduledTask -TaskName "ADIPACK MDM Agent" -Action $action -Trigger $trigger -Principal $princ -Settings $set -Force | Out-Null
Write-Host "Agente instalado. Se iniciará en el próximo inicio de sesión (o ejecuta la tarea 'ADIPACK MDM Agent')."
