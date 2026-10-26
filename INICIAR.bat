@echo off
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 gpu_watts.py
    if errorlevel 1 pause
    exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
    python gpu_watts.py
    if errorlevel 1 pause
    exit /b
)
echo Instala Python 3 de 64 bits desde https://www.python.org/downloads/windows/
echo Incluye Tcl/Tk durante la instalacion. Despues vuelve a abrir INICIAR.bat.
pause
