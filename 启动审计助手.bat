@echo off
title AuditHelper - Streamlit
cd /d "C:\AuditHelper"
echo ================================================
echo  AuditHelper - starting web UI (Streamlit)
echo  URL: http://localhost:8501
echo  Stop: close this window or press Ctrl+C
echo ================================================
echo.
".venv\Scripts\python.exe" -m streamlit run app.py
echo.
echo Streamlit exited. If an error message appeared above, take a screenshot.
pause
