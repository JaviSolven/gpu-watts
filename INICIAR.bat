@echo off
cd /d "%~dp0"
rem pyw / pythonw: Python sin ventana de consola, asi solo queda GPU Watts.
rem Si algo falla, el error sale en un aviso y en %LOCALAPPDATA%\GPU_Watts\errores.log
where pyw >nul 2>nul
if not errorlevel 1 (
    start "" pyw -3 gpu_watts.py
    exit /b
)
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw gpu_watts.py
    exit /b
)
echo Instala Python 3 de 64 bits desde https://www.python.org/downloads/windows/
echo Incluye Tcl/Tk durante la instalacion. Despues vuelve a abrir INICIAR.bat.
pause
