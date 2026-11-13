@echo off
REM Convenience launcher for langpatch.py (uses py -X utf8 when available).
setlocal
where py >nul 2>nul
if %errorlevel%==0 (
    py -X utf8 "%~dp0langpatch.py" %*
) else (
    python "%~dp0langpatch.py" %*
)
