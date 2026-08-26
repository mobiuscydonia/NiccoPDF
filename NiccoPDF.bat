@echo off
rem NiccoPDF launcher — double-click, or drop a PDF onto this file.
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "%~dp0app.py" %*
) else (
  start "" python "%~dp0app.py" %*
)
