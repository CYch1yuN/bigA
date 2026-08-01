#Requires -Version 5.1
<#
.SYNOPSIS
    注册 Phase 4 自动化所需的 Windows 任务计划（每日盘后 + 每周六汇总）。

.DESCRIPTION
    本脚本只做一件事：用 schtasks 注册两个计划任务，动作指向 scripts/run_daily.ps1
    与 scripts/run_weekly.ps1。这两个包装脚本最终调用
    `ashare-quant automation daily|weekly`——只产出研究信号与模拟账户报告，
    不连接券商、不涉及真实资金。

.PARAMETER TaskPrefix
    任务名前缀（默认 AShareQuantAutomation）。

.PARAMETER PythonExe
    Python 解释器路径；默认 <RepoRoot>\.venv\Scripts\python.exe。

.PARAMETER RepoRoot
    仓库根目录；默认取本脚本上级目录。

.PARAMETER DailyTime
    每日任务触发时间（HH:MM，默认 18:30）。

.PARAMETER WeeklyDay
    每周任务触发星期（MON..SUN，默认 SAT）。

.PARAMETER WeeklyTime
    每周任务触发时间（HH:MM，默认 09:00）。

.PARAMETER RunLevel
    运行级别（LIMITED / HIGHEST，默认 LIMITED）。

.PARAMETER Force
    注册前先删除同名任务（覆盖式注册）。

.PARAMETER WhatIf
    仅打印将要执行的 schtasks 命令，不实际注册（PowerShell 内置 -WhatIf）。

.EXAMPLE
    .\install_scheduler.ps1 -WhatIf
    .\install_scheduler.ps1 -Force
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$TaskPrefix = "AShareQuantAutomation",
    [string]$PythonExe = "",
    [string]$RepoRoot = "",
    [string]$DailyTime = "18:30",
    [string]$WeeklyDay = "SAT",
    [string]$WeeklyTime = "09:00",
    [string]$RunLevel = "LIMITED",
    [switch]$Force
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
if (-not $PythonExe) {
    $PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path $PythonExe)) {
    throw "找不到 Python 解释器: $PythonExe`n请先在仓库内创建 .venv 并安装依赖。"
}

$runDaily = Join-Path $RepoRoot "scripts\run_daily.ps1"
$runWeekly = Join-Path $RepoRoot "scripts\run_weekly.ps1"
if (-not (Test-Path $runDaily)) { throw "找不到启动脚本: $runDaily" }
if (-not (Test-Path $runWeekly)) { throw "找不到启动脚本: $runWeekly" }

$dailyTask = "$TaskPrefix-Daily"
$weeklyTask = "$TaskPrefix-Weekly"

function Register-Task {
    param(
        [string]$Name,
        [string]$Schedule,
        [string]$Time,
        [string]$Day,
        [string]$Script
    )
    $action = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Script`""
    $cmd = "schtasks /Create /TN `"$Name`" /SC $Schedule"
    if ($Schedule -eq "WEEKLY") { $cmd += " /D $Day" }
    $cmd += " /ST $Time /RL $RunLevel /TR `"$action`" /F"
    Write-Host "COMMAND: $cmd"

    if ($Force -and $PSCmdlet.ShouldProcess($Name, "删除旧任务（Force）")) {
        schtasks /Delete /TN "$Name" /F 2>$null | Out-Null
    }
    if ($PSCmdlet.ShouldProcess($Name, "注册计划任务")) {
        # 注意：DAILY 任务不得带 /D（schtasks 对 /SC DAILY 不接受 /D 参数）；
        # 旧实现无条件附加 /D $Day（Daily 传 $null）导致
        # "Invalid syntax. Value expected for '/D'" 而创建失败。
        if ($Schedule -eq "WEEKLY") {
            schtasks /Create /TN "$Name" /SC $Schedule /D $Day /ST $Time /RL $RunLevel /TR "$action" /F
        } else {
            schtasks /Create /TN "$Name" /SC $Schedule /ST $Time /RL $RunLevel /TR "$action" /F
        }
    }
}

Write-Host "仓库根目录: $RepoRoot"
Write-Host "Python 解释器: $PythonExe"
Register-Task $dailyTask "DAILY" $DailyTime $null $runDaily
Register-Task $weeklyTask "WEEKLY" $WeeklyTime $WeeklyDay $runWeekly

if (-not $PSCmdlet.ShouldProcess) {
    Write-Host "(WhatIf) 以上为预览，未实际注册。去掉 -WhatIf 以执行。"
} else {
    Write-Host "已注册任务: $dailyTask, $weeklyTask"
}
