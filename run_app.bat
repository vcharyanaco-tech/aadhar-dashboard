@echo off
cd /d "%~dp0"
REM To let other computers on the network open the app, add:  --server.address 0.0.0.0
REM Preferred: the "py" launcher, which is the one that tracks the real install.
REM The earlier `where py && (...) || (...)` form ran the app twice whenever py
REM existed but streamlit failed, because the `||` branch also fired.
where py >nul 2>nul
if %errorlevel%==0 (
    py -m streamlit run app.py
) else (
    python -m streamlit run app.py
)
echo.
echo The app has stopped. Read any error message above.
pause
