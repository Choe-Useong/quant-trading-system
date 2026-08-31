param(
    [ValidateSet("install", "status", "run", "disable", "enable", "remove")]
    [string]$Action = "status",

    [ValidateSet("preview", "live")]
    [string]$Mode = "preview",

    [ValidateSet("repeating", "daily")]
    [string]$Schedule = "daily",

    [ValidateRange(1, 1440)]
    [int]$EveryMinutes = 5,

    [string]$DailyAt = "09:10",

    [string]$TaskName = "",

    [string]$ProfileJson = "",

    [string]$LimitBufferPct = "0.00",

    [ValidateSet("empty-account", "always", "never")]
    [string]$BootstrapPolicy = "empty-account",

    [ValidateSet("limit", "market")]
    [string]$OrderStyle = "limit",

    [switch]$Force
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$wrapper = Join-Path $PSScriptRoot "run_kr_stock_live_auto.cmd"

if (-not (Test-Path -LiteralPath $wrapper)) {
    throw "Missing scheduler wrapper: $wrapper"
}

if ([string]::IsNullOrWhiteSpace($TaskName)) {
    $modeLabel = (Get-Culture).TextInfo.ToTitleCase($Mode.ToLowerInvariant())
    $TaskName = "CoinProject KR ETF ISA $modeLabel"
}

function Get-NextStartAt {
    $now = Get-Date
    return $now.AddMinutes(1)
}

function Resolve-DailyAt {
    param([string]$TimeValue)
    if ($TimeValue -notmatch '^\d{1,2}:\d{2}$') {
        throw "DailyAt must be HH:mm, got: $TimeValue"
    }
    $parts = $TimeValue.Split(":")
    $hour = [int]$parts[0]
    $minute = [int]$parts[1]
    if ($hour -lt 0 -or $hour -gt 23 -or $minute -lt 0 -or $minute -gt 59) {
        throw "DailyAt must be HH:mm in 00:00-23:59, got: $TimeValue"
    }
    $now = Get-Date
    $startAt = Get-Date -Year $now.Year -Month $now.Month -Day $now.Day -Hour $hour -Minute $minute -Second 0
    if ($startAt -le $now) {
        $startAt = $startAt.AddDays(1)
    }
    return $startAt
}

function Resolve-ProjectPath {
    param([string]$PathValue)
    if ([string]::IsNullOrWhiteSpace($PathValue)) {
        return ""
    }
    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return (Resolve-Path -LiteralPath $PathValue).Path
    }
    return (Resolve-Path -LiteralPath (Join-Path $projectRoot $PathValue)).Path
}

function Quote-TaskArg {
    param([string]$Value)
    return "`"$Value`""
}

function Write-TaskSummary {
    param([string]$TaskName)
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "Task not found: $TaskName"
        return
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    $action = $task.Actions | Select-Object -First 1
    $trigger = $task.Triggers | Select-Object -First 1

    Write-Host "TaskName: $TaskName"
    Write-Host "State: $($task.State)"
    Write-Host "NextRunTime: $($info.NextRunTime)"
    Write-Host "LastRunTime: $($info.LastRunTime)"
    Write-Host "LastTaskResult: $($info.LastTaskResult)"
    Write-Host "Execute: $($action.Execute)"
    Write-Host "Arguments: $($action.Arguments)"
    Write-Host "WorkingDirectory: $($action.WorkingDirectory)"
    Write-Host "TriggerStartBoundary: $($trigger.StartBoundary)"
    if ($null -ne $trigger.Repetition) {
        Write-Host "RepetitionInterval: $($trigger.Repetition.Interval)"
    } else {
        Write-Host "RepetitionInterval: "
    }
}

switch ($Action) {
    "install" {
        if ($Schedule -eq "daily") {
            $startAt = Resolve-DailyAt -TimeValue $DailyAt
        } else {
            $startAt = Get-NextStartAt
        }
        $resolvedProfileJson = Resolve-ProjectPath -PathValue $ProfileJson
        $wrapperArgParts = @($Mode)
        if (-not [string]::IsNullOrWhiteSpace($resolvedProfileJson)) {
            $wrapperArgParts += (Quote-TaskArg -Value $resolvedProfileJson)
        }
        if ($Mode -eq "live") {
            if ([string]::IsNullOrWhiteSpace($resolvedProfileJson)) {
                $defaultProfileJson = Resolve-ProjectPath -PathValue "kr_stock_live/configs/kr_etf_cat24_rank9_top2_w8020_breadth45_isa.json"
                $wrapperArgParts += (Quote-TaskArg -Value $defaultProfileJson)
            }
            $wrapperArgParts += $LimitBufferPct
            $wrapperArgParts += $BootstrapPolicy
            $wrapperArgParts += $OrderStyle
        }
        $wrapperArgs = $wrapperArgParts -join " "
        $actionSpec = New-ScheduledTaskAction `
            -Execute $wrapper `
            -Argument $wrapperArgs `
            -WorkingDirectory $projectRoot

        if ($Schedule -eq "daily") {
            $trigger = New-ScheduledTaskTrigger -Daily -At $startAt
        } else {
            $trigger = New-ScheduledTaskTrigger `
                -Once `
                -At $startAt `
                -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
                -RepetitionDuration (New-TimeSpan -Days 3650)
        }

        $settings = New-ScheduledTaskSettingsSet `
            -StartWhenAvailable `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries

        $registerParams = @{
            TaskName = $TaskName
            Action = $actionSpec
            Trigger = $trigger
            Settings = $settings
            Description = "Multi-asset quant KR stock/ETF live scheduler ($Mode, $Schedule) via $wrapper"
        }
        if ($Force) {
            $registerParams["Force"] = $true
        }

        Register-ScheduledTask @registerParams | Out-Null
        Write-Host "Installed task: $TaskName"
        Write-TaskSummary -TaskName $TaskName
    }
    "status" {
        Write-TaskSummary -TaskName $TaskName
    }
    "run" {
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "Started task: $TaskName"
        Write-TaskSummary -TaskName $TaskName
    }
    "disable" {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        Write-Host "Disabled task: $TaskName"
        Write-TaskSummary -TaskName $TaskName
    }
    "enable" {
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
        Write-Host "Enabled task: $TaskName"
        Write-TaskSummary -TaskName $TaskName
    }
    "remove" {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed task: $TaskName"
    }
}
