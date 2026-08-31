param(
    [ValidateSet("install", "status", "run", "disable", "enable", "remove")]
    [string]$Action = "status",

    [ValidateSet("preview", "live")]
    [string]$Mode = "preview",

    [ValidateRange(0, 59)]
    [int]$Minute = 2,

    [string]$TaskName = "",

    [switch]$Force
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$wrapper = Join-Path $PSScriptRoot "run_live_job_v2.cmd"

if (-not (Test-Path -LiteralPath $wrapper)) {
    throw "Missing scheduler wrapper: $wrapper"
}

if ([string]::IsNullOrWhiteSpace($TaskName)) {
    $modeLabel = (Get-Culture).TextInfo.ToTitleCase($Mode.ToLowerInvariant())
    $TaskName = "CoinProject 4core V2 $modeLabel"
}

function Get-NextStartAt {
    param([int]$Minute)
    $now = Get-Date
    $candidate = Get-Date -Hour $now.Hour -Minute $Minute -Second 0
    if ($candidate -le $now) {
        $candidate = $candidate.AddHours(1)
    }
    return $candidate
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
}

switch ($Action) {
    "install" {
        $startAt = Get-NextStartAt -Minute $Minute
        $actionSpec = New-ScheduledTaskAction `
            -Execute $wrapper `
            -Argument $Mode `
            -WorkingDirectory $projectRoot

        $trigger = New-ScheduledTaskTrigger `
            -Once `
            -At $startAt `
            -RepetitionInterval (New-TimeSpan -Hours 1) `
            -RepetitionDuration (New-TimeSpan -Days 3650)

        $settings = New-ScheduledTaskSettingsSet `
            -StartWhenAvailable `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries

        $registerParams = @{
            TaskName = $TaskName
            Action = $actionSpec
            Trigger = $trigger
            Settings = $settings
            Description = "Multi-asset quant crypto live scheduler ($Mode) via $wrapper"
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
