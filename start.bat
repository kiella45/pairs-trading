@echo off
cd /d "%~dp0"
start "Backend" cmd /k "cd backend && python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000"
timeout /t 3
start "Frontend" cmd /k "cd frontend && python -m http.server 3000"
timeout /t 2
start http://localhost:3000
