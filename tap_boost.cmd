@echo off
chcp 65001 >nul
py "%~dp0scripts\tap_boost.py" --count 10000 --rate 200 %*
pause
