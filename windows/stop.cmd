@echo off
rem Wrapper: requires "conda activate cg" (checked by primer.cmd and primer_local.py).
call "%~dp0primer.cmd" stop %*
exit /b %ERRORLEVEL%
