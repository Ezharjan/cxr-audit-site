@echo off
rem PRIMER-LLM Windows local launcher. Run from an Anaconda Prompt after: conda activate cg
setlocal
if not "%CONDA_DEFAULT_ENV%"=="cg" goto needcg
if "%CONDA_PREFIX%"=="" goto needcg
if not exist "%CONDA_PREFIX%\python.exe" goto needcg
"%CONDA_PREFIX%\python.exe" "%~dp0primer_local.py" %*
exit /b %ERRORLEVEL%
:needcg
echo [PRIMER] ERROR: the Conda environment "cg" is not active.
echo [PRIMER] Run:  conda activate cg    then run this script again.
echo [PRIMER] Current CONDA_DEFAULT_ENV=%CONDA_DEFAULT_ENV%
exit /b 3
