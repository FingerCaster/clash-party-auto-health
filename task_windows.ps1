[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('Export','Register','Remove','Restore')][string]$Action,
    [Parameter(Mandatory=$true)][string]$TaskName,
    [string]$Pythonw,
    [string]$Daemon,
    [string]$DataDir,
    [string]$LogFile,
    [string]$XmlFile
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
switch ($Action) {
    'Export' {
        if ($existing) { Export-ScheduledTask -TaskName $TaskName }
    }
    'Remove' {
        if ($existing) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
    }
    'Restore' {
        Register-ScheduledTask -TaskName $TaskName -Xml (Get-Content -LiteralPath $XmlFile -Raw -Encoding UTF8) -Force | Out-Null
    }
    'Register' {
        foreach ($value in @($Pythonw, $Daemon, $DataDir, $LogFile)) {
            if (-not $value -or $value.Contains('"')) { throw 'Invalid task path.' }
        }
        $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $arguments = '"{0}" --data-dir "{1}" --log-file "{2}"' -f $Daemon, $DataDir, $LogFile
        $taskAction = New-ScheduledTaskAction -Execute $Pythonw -Argument $arguments -WorkingDirectory (Split-Path -Parent $Daemon)
        $logon = New-ScheduledTaskTrigger -AtLogOn -User $user
        $periodic = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 1)
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger @($logon, $periodic) -Principal $principal -Settings $settings -Description 'Clash Party automatic node quarantine and recovery' -Force | Out-Null
    }
}
