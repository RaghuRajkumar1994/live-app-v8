@echo off
cd /d "%~dp0"
:: Launches server.py using conhost.exe (Legacy Console) to ensure the Close (X) button can be disabled.
start conhost.exe python server.py
exit