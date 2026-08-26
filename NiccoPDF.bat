@echo off
rem NiccoPDF launcher (Windows) — double-click, or drop a PDF onto this file.
setlocal
where python >nul 2>nul
if errorlevel 1 goto nopython

rem first-run setup: install the PDF components if they are missing
python -c "import pymupdf, PIL, tkinter" >nul 2>nul
if errorlevel 1 (
  echo First-run setup: installing NiccoPDF components ^(about a minute^)...
  python -m pip install --user pymupdf pymupdf-fonts pillow
  python -c "import pymupdf, PIL, tkinter" >nul 2>nul
  if errorlevel 1 (
    echo.
    echo Setup failed. Please run:  python -m pip install pymupdf pymupdf-fonts pillow
    pause
    exit /b 1
  )
)

where pythonw >nul 2>nul
if errorlevel 1 (
  start "" python "%~dp0app.py" %*
) else (
  start "" pythonw "%~dp0app.py" %*
)
exit /b 0

:nopython
echo NiccoPDF needs Python 3. Opening the download page...
echo After installing ^(tick "Add python.exe to PATH"^), double-click NiccoPDF.bat again.
start https://www.python.org/downloads/
pause
exit /b 1
