<#
.SYNOPSIS
  Runs "SuperDownloader.exe --selftest <report>" with a time limit and prints the report.
  A --windowed exe that fails to start shows an error dialog and would wait forever,
  so the process is killed after -TimeoutSeconds.
#>
param(
    [string]$Exe = (Join-Path (Split-Path -Parent $PSScriptRoot) 'dist\SuperDownloader.exe'),
    [string]$Report = (Join-Path (Split-Path -Parent $PSScriptRoot) 'build\selftest.json'),
    [int]$TimeoutSeconds = 300
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Report) | Out-Null
if (Test-Path $Report) { Remove-Item -Force $Report }
$sw = [Diagnostics.Stopwatch]::StartNew()
$p = Start-Process -FilePath $Exe -ArgumentList @('--selftest', "`"$Report`"") -PassThru
if (-not $p.WaitForExit($TimeoutSeconds * 1000)) {
    $p.Kill()
    Write-Host "Self-test timed out after $TimeoutSeconds s."
    if (Test-Path $Report) { Get-Content $Report }
    exit 3
}
$p.WaitForExit()
Write-Host ("Self-test finished in {0:N1} s with exit code {1}" -f $sw.Elapsed.TotalSeconds, $p.ExitCode)
if (Test-Path $Report) { Get-Content $Report } else { Write-Host 'No report was written.' }
exit $p.ExitCode
