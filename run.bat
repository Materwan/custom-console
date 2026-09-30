@echo off
rem Launch the console in its own window. Works from a source checkout
rem (no `pip install` needed) and prefers the project's virtualenv if present.
set "ROOT=%~dp0"
set "PYTHONPATH=%ROOT%src;%PYTHONPATH%"
if exist "%ROOT%.venv\Scripts\python.exe" (
    start "Custom Console" cmd /k ""%ROOT%.venv\Scripts\python.exe" -m custom_console"
) else (
    start "Custom Console" cmd /k python -m custom_console
)
