@echo off
REM 建独立虚拟环境并装依赖。全程离线，OCR 模型已经打在 rapidocr 的 wheel 里。
setlocal
cd /d "%~dp0"

echo [1/3] 建虚拟环境 .venv
if not exist .venv (
    python -m venv .venv || goto :err
) else (
    echo      已存在，跳过
)

echo [2/3] 升级 pip
call .venv\Scripts\python.exe -m pip install -q --upgrade pip || goto :err

echo [3/3] 安装依赖（约 150MB，opencv 和 onnxruntime 占大头）
call .venv\Scripts\python.exe -m pip install -r requirements.txt || goto :err

echo.
echo 装好了。下一步：
echo   列出可见窗口：  .venv\Scripts\python.exe main.py --list
echo   校准抓取范围：  .venv\Scripts\python.exe main.py -w "游戏名" --snapshot crop.png
echo   正式启动：      .venv\Scripts\python.exe main.py -w "游戏名" --region 0.05,0.60,0.95,0.88
exit /b 0

:err
echo.
echo 安装失败，看上面的报错。
echo 如果卡在某个包没有 cp313 的 wheel，可以改用 Python 3.12 重建 venv。
exit /b 1
