@echo off
chcp 65001 >nul
:: 仓库可整体搬迁：根目录由本文件位置推导，Python 走 PATH
:: 若本机 python 不是带依赖的那个，可设置 PY_EXE 覆盖，例如：
::     set PY_EXE=C:\path\to\python.exe
cd /d "%~dp0"
if not defined PY_EXE set "PY_EXE=python"

echo ============================================
echo  备件价格中台 - 刷新后端 + 重新抓取真实数据
echo ============================================

:: 1) 停掉占用 8000 端口的旧后端进程
echo [1/3] 停止旧后端 ...
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":8000 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
)
timeout /t 2 >nul

:: 2) 重新抓取真实数据（全品牌全国家）
::    OPPO / Google 等若被墙，请先在本窗口设置代理再运行，例如：
::        set HTTPS_PROXY=http://127.0.0.1:7890
::        set HTTP_PROXY=http://127.0.0.1:7890
echo [2/3] 开始全量抓取真实备件价格（可能需要数分钟）...
"%PY_EXE%" -m crawler.run

:: 3) 重启后端（后台运行，不阻塞）
echo [3/3] 启动最新版后端 ...
start "" "%PY_EXE%" api/server.py

echo.
echo 完成！请在浏览器刷新预览面板（http://localhost:8000 由 Web 预览代理转发）。
echo 如需只刷新后端不重抓：直接双击本文件即可（抓取步骤很快，增量 upsert 不会清空旧数据）。
pause
