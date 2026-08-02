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

.PARAMETER SchtasksExe
    schtasks 可执行文件路径；默认 "schtasks"。测试时可注入假实现
    （记录参数、模拟失败），禁止操作真实任务。

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
    [string]$SchtasksExe = "schtasks",
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

# ---------------------------------------------------------------------- #
# 统一 schtasks 调用：预览与执行共用同一参数数组，杜绝两套逻辑分叉；
# 每次调用后检查 $LASTEXITCODE，非零立即 throw（fail-fast）。
# ---------------------------------------------------------------------- #

function Invoke-Schtasks {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$ArgList
    )
    # 注意：COMMAND 预览由调用方（Register-Task）在构造参数数组后、ShouldProcess
    # 之前打印，保证 -WhatIf 模式下也能看到完整命令。此处只执行并检查退出码。
    # $ErrorActionPreference='Stop' 会把非零外部命令退出码提升为 NativeCommandError，
    # 故执行前局部降级，改用 $LASTEXITCODE 显式判断。
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $SchtasksExe @ArgList
        $code = $LASTEXITCODE
    } catch {
        $code = 1
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    if ($code -ne 0) {
        $cmdline = "schtasks " + ($ArgList -join " ")
        throw "schtasks 执行失败（exit $code）: $cmdline"
    }
    return $code
}

function Task-Exists {
    param([string]$Name)
    # schtasks /Query 对不存在的任务返回非零，PowerShell 5.1 在
    # $ErrorActionPreference='Stop' 下会把非零外部命令退出码提升为
    # NativeCommandError 并中断脚本——因此这里必须局部降级错误偏好。
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $SchtasksExe /Query /TN "$Name" 2>$null | Out-Null
        $exists = ($LASTEXITCODE -eq 0)
    } catch {
        $exists = $false
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    return $exists
}

function Register-Task {
    param(
        [string]$Name,
        [string]$Schedule,
        [string]$Time,
        [string]$Day,
        [string]$Script
    )
    $action = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Script`""

    # 唯一参数来源：Daily 不得带 /D，Weekly 才带 /D <Day>。
    # 预览（COMMAND: 打印）与实际执行共用 $createArgs，保证不会分叉。
    $createArgs = @(
        "/Create",
        "/TN", $Name,
        "/SC", $Schedule
    )
    if ($Schedule -eq "WEEKLY" -and $Day) {
        $createArgs += @("/D", $Day)
    }
    $createArgs += @(
        "/ST", $Time,
        "/RL", $RunLevel,
        "/TR", $action,
        "/F"
    )

    # 参数数组构造完成后、ShouldProcess 之前打印命令——
    # 使 -WhatIf 模式（不执行任何 schtasks）也能审计完整参数。
    Write-Host "COMMAND: schtasks $($createArgs -join ' ')"

    if ($Force -and $PSCmdlet.ShouldProcess($Name, "删除旧任务（Force）")) {
        if (Task-Exists $Name) {
            Invoke-Schtasks @("/Delete", "/TN", $Name, "/F")
        } else {
            Write-Host "任务不存在，跳过删除: $Name"
        }
    }

    if ($PSCmdlet.ShouldProcess($Name, "注册计划任务")) {
        Invoke-Schtasks $createArgs
        if (-not (Task-Exists $Name)) {
            throw "注册后验证失败：任务未创建 $Name"
        }
    }
}

Write-Host "仓库根目录: $RepoRoot"
Write-Host "Python 解释器: $PythonExe"

$failures = @()
try {
    Register-Task $dailyTask "DAILY" $DailyTime $null $runDaily
    Register-Task $weeklyTask "WEEKLY" $WeeklyTime $WeeklyDay $runWeekly
} catch {
    $failures += $_
}

if ($WhatIfPreference) {
    Write-Host "(WhatIf) 以上为预览，未实际注册。去掉 -WhatIf 以执行。"
    exit 0
}

# 注册结束后统一验证两个任务均存在（不依赖中途输出）。
foreach ($t in @($dailyTask, $weeklyTask)) {
    if (-not (Task-Exists $t)) {
        $failures += "任务未注册: $t"
    }
}

if ($failures.Count -gt 0) {
    Write-Host "安装失败："
    foreach ($f in $failures) { Write-Host "  - $f" }
    # 部分安装：列出已成功注册的任务，供人工清理，绝不打印整体成功。
    $registered = @($dailyTask, $weeklyTask) | Where-Object { Task-Exists $_ }
    if ($registered.Count -gt 0) {
        Write-Host "部分安装：以下任务已注册（需人工处理）: $($registered -join ', ')"
    }
    exit 1
}

Write-Host "已注册任务: $dailyTask, $weeklyTask"
exit 0
