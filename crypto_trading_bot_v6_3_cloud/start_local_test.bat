@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
set DATA_DIR=%~dp0data
python -m streamlit run cloud_app.py --server.address=0.0.0.0 --server.port=8508
pause
