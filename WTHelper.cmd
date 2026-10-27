@echo off
set "PYTHONIOENCODING=utf-8"
pyw "%~dp0scripts\gui.py" --elevate
if errorlevel 1 (
  py "%~dp0scripts\gui.py"
  pause
)
