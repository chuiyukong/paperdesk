@echo off
cd /d "%~dp0"
where pyw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pyw.exe -3.12 "%~dp0launch.py"
    exit /b
)
where pythonw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pythonw.exe "%~dp0launch.py"
    exit /b
)
powershell.exe -NoProfile -Command "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('Python 3.12 is required. Please install Python and run this launcher again.','PaperDesk')"
