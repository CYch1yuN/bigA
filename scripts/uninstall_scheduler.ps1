<Requires -Version 5.1>
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
