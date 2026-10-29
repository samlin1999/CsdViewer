@echo off
setlocal
chcp 65001 >nul
title csdview - Cocos Studio .csd viewer
cd /d "%~dp0"

rem This file is deliberately ASCII-only. All Chinese messages are printed by
rem Python instead, because cmd.exe loses sync while parsing a UTF-8 batch file
rem that contains non-ASCII text and ends up executing half-lines as commands.

rem ---------------------------------------------------------------- Python
set "PY="
python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=python"

if not defined PY (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY goto :nopython

rem ------------------------------------------------------------- dependencies
rem Installing with -e also registers the "csdview" command, so AI agents can
rem call the tool without hard-coding any path.
%PY% -c "import PIL, numpy" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   First run: installing csdview and its dependencies...
    echo.
    %PY% -m pip install -e .
    if errorlevel 1 goto :nodeps
)

rem ---------------------------------------------------------------- launch
set "PORT=%~2"
if not defined PORT set "PORT=8770"

if "%~1"=="" (
    %PY% csdview.py ui --port %PORT%
) else (
    %PY% csdview.py ui "%~1" --port %PORT%
)
if errorlevel 1 pause
goto :end

rem ---------------------------------------------------------------- errors
:nopython
echo.
echo   Python not found.
echo.
echo   Please install Python 3.9 or newer:  https://www.python.org/downloads/
echo   Remember to tick "Add Python to PATH" during the installation.
echo.
pause
goto :end

:nodeps
echo.
echo   Installation failed. Please run this manually:
echo       pip install -e .
echo.
pause
goto :end

:end
endlocal
