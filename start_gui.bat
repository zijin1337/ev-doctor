@echo off
chcp 65001 >nul
cd /d %~dp0
rem 电诊桌面版：第一次运行会自动安装界面库 PySide6（约 70 MB），之后双击直接打开
where py >nul 2>nul && (set "PY=py -3" & set "PYW=pyw -3") || (set "PY=python" & set "PYW=pythonw")
%PY% -c "import PySide6" >nul 2>nul || (
  echo 第一次运行桌面版，正在安装界面库 PySide6（约 70 MB）...
  %PY% -m pip install PySide6-Essentials || (echo 安装失败，请检查网络后重试 & pause & exit /b 1)
)
start "" %PYW% gui.py
