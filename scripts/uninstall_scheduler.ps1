#Requires -Version 5.1
<#
.SYNOPSIS
    注销 Phase 4 自动化注册的 Windows 任务计划。

.PARAMETER TaskPrefix
    任务名前缀（默认 AShareQuantAutomation）。

.PARAMETER RepoRoot
    仓库根目录；默认取本脚本上级目录。

.PARAMETER WhatIf
    仅打印将要执行的 schtasks 命令，不实际注销（PowerShell 内置 -WhatIf）。

.EXAMPLE
    .\uninstall_scheduler.ps1 -WhatIf
    .\uninstall_scheduler.ps1
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$TaskPrefix = "AShareQuantAutomation",
    [string]$RepoRoot = ""
)

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


if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

$dailyTask = "$TaskPrefix-Daily"
$weeklyTask = "$TaskPrefix-Weekly"

foreach ($name in @($dailyTask, $weeklyTask)) {
    Write-Host "COMMAND: schtasks /Delete /TN `"$name`" /F"
    if ($PSCmdlet.ShouldProcess($name, "注销计划任务")) {
        $out = schtasks /Delete /TN "$name" /F 2>&1
        Write-Host $out
    }
}

if (-not $PSCmdlet.ShouldProcess) {
    Write-Host "(WhatIf) 以上为预览，未实际注销。去掉 -WhatIf 以执行。"
} else {
    Write-Host "已尝试注销任务: $dailyTask, $weeklyTask"
}
