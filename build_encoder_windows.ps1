$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($env:OS -ne "Windows_NT") {
    throw "请在 Windows 上运行此脚本，PyInstaller 不支持从其他操作系统交叉编译 Windows 程序。"
}

py -3.12 -m venv .venv-encoder-build
if ($LASTEXITCODE -ne 0) { throw "无法创建 Python 3.12 虚拟环境。" }
$python = Join-Path $PSScriptRoot ".venv-encoder-build\Scripts\python.exe"
& $python -m pip install -r requirements-encoder-build.txt
if ($LASTEXITCODE -ne 0) { throw "安装构建依赖失败。" }
& $python -m unittest discover -s tests -p test_encoder_core.py
if ($LASTEXITCODE -ne 0) { throw "核心测试失败，停止打包。" }
& $python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name EncoderMonitor_V0.4 --icon "web/comau-logo.ico" `
    --add-data "web/comau-logo.png;web" encoder_dashboard.py
if ($LASTEXITCODE -ne 0) { throw "打包失败。" }
Write-Host "完成：$PSScriptRoot\dist\EncoderMonitor_V0.4.exe"
