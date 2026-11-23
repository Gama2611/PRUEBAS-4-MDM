@echo off
chcp 65001 >nul
cd /d "%~dp0server"
"..\venv\Scripts\python.exe" respaldar_bd.py %*
if "%1"=="" pause
