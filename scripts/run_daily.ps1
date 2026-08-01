<#
.SYNOPSIS
    每日自动化启动包装脚本（供 Windows 任务计划调用）。

.DESCRIPTION
    解析仓库根目录，设置 PYTHONPATH，调用 `ashare-quant automation daily`。
    支持透传参数（如 -DryRun / -Date 2026-07-31 / -Config <path>）。

    安全边界：本脚本只触发研究信号与模拟账户流程，不连接券商、不涉及真实资金。
#>
$ErrorActionPreference = 'Stop'

# --- 编码自检（必须紧跟 ErrorActionPreference） ------------------------- #
# 本文件以 UTF-8 with BOM 保存：Windows PowerShell 5.1 缺少 BOM 时会按系统
# ANSI 代码页（简体中文 = GBK）解析源文件，中文字面量会退化成乱码。
# BOM 让 5.1 与 7.x 都走 UTF-8 分支。下面几行进一步保证子进程输出也按
# UTF-8 呈现：Python 端固定用 UTF-8 写 stdout，控制台若停在 GBK 代码页
# 同样会糊成乱码。
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$env:PYTHONPATH = (Join-Path $RepoRoot "src")
$config = Join-Path $RepoRoot "config\automation.default.yaml"

if (-not (Test-Path $PythonExe)) {
    throw "找不到 Python 解释器: $PythonExe"
}

& $PythonExe -m ashare_quant.cli automation daily --config "$config" @args
exit $LASTEXITCODE
