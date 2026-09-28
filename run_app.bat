@echo off
cd /d "%~dp0"
REM To let other computers on the network open the app, add:  --server.address 0.0.0.0
where py >nul 2>nul && (py -m streamlit run app.py) || (python -m streamlit run app.py)
echo.
echo The app has stopped. Read any error message above.
pause
