@echo off
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
py "%~dp0scripts\auto_chest_supervisor.py" %*
if errorlevel 130 exit /b 130
if errorlevel 1 pause
