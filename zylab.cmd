@echo off
setlocal
set "HERE=%~dp0"

rem Windows 上 `./zylab` 跑不起来：cmd / PowerShell 不认 shebang，而那个文件
rem 没有扩展名。这个 .cmd 就是它在 Windows 的对应物 —— 同样只做转发，不复制
rem 任何逻辑，永远不会与 zylab.py 产生偏斜。
rem
rem 找解释器有两个 Windows 专属的坑：
rem   1. `python3` 这个名字基本不存在 —— python.org 的安装器只装 python.exe
rem      和 py.exe，shebang 里的 `/usr/bin/env python3` 在这里帮不上忙。
rem   2. 没装 Python 时 PATH 上**仍然有** python.exe：那是微软商店的"应用
rem      执行别名"，运行它会静默弹出商店页面，既不报错也不执行任何东西。
rem 所以两条都先用一次真实的版本探测验明正身，再决定用哪个。

py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 (
    py -3 "%HERE%zylab.py" %*
    exit /b %errorlevel%
)

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 (
    python "%HERE%zylab.py" %*
    exit /b %errorlevel%
)

echo 找不到 Python 3.10 或更新的版本。
echo.
echo   还没装：https://www.python.org/downloads/windows/
echo           安装时勾上 "Add python.exe to PATH"。
echo.
echo   已经装了却还看到这条：PATH 上的 python.exe 多半是微软商店的应用执行
echo   别名。设置 ^> 应用 ^> 高级应用设置 ^> 应用执行别名，把 python.exe 和
echo   python3.exe 都关掉，然后重开一个终端。
echo.
echo   另外 Bash 工具需要一个真 bash：装 Git for Windows
echo   ^(https://git-scm.com/download/win^)，它自带 bash.exe。
echo   注意 System32\bash.exe 不算 —— 那是 WSL 启动器，zylab 会拒绝用它。
exit /b 1
