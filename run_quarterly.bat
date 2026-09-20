@echo off
:: 仓库可整体搬迁：根目录由本文件位置推导
cd /d "%~dp0"
python run_quarterly.py >> quarterly.log 2>&1
