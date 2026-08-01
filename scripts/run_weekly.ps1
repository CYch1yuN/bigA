<#
.SYNOPSIS
    每周汇总自动化启动包装脚本（供 Windows 任务计划调用）。

.DESCRIPTION
    解析仓库根目录，设置 PYTHONPATH，调用 `ashare-quant automation weekly`。
    支持透传参数（如 -DryRun / -Date 2026-08-01 / -Config <path>）。

    安全边界：本脚本只触发研究信号与模拟账户汇总，不连接券商、不涉及真实资金。
#>
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$env:PYTHONPATH = (Join-Path $RepoRoot "src")
$config = Join-Path $RepoRoot "config\automation.default.yaml"

if (-not (Test-Path $PythonExe)) {
    throw "找不到 Python 解释器: $PythonExe"
}

& $PythonExe -m ashare_quant.cli automation weekly --config "$config" @args
exit $LASTEXITCODE
