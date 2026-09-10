param([string]$ScriptsPath = (Join-Path $PSScriptRoot '../../packaging/windows/scripts'))

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
if ($env:OS -cne 'Windows_NT') {
    Write-Host 'Windows batch entry point tests skipped on a non-Windows host.'
    return
}

$fixture = Join-Path $env:TEMP ('Redis Batch ' + [char]0x6D4B + ' %! ' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $fixture | Out-Null
try {
    foreach ($name in @('Install-Redis', 'Update-Redis', 'Uninstall-Redis', 'Purge-Redis', 'Start-Redis')) {
        Copy-Item -LiteralPath (Join-Path $ScriptsPath "$name.bat") -Destination $fixture
    }
    $stub = @'
param([switch]$Purge, [switch]$ConfirmPurge, [string]$Lang, [switch]$FromBatch, [switch]$Help)
if ($Purge -and $ConfirmPurge -and (Read-Host 'Continue? [Y/N]') -ine 'Y') { exit 0 }
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'called.txt'), ($MyInvocation.MyCommand.Name + ':' + $Purge.IsPresent + ':' + $Lang + ':' + $FromBatch.IsPresent + ':' + $Help.IsPresent))
exit 7
'@
    foreach ($name in @('Install-Redis', 'Update-Redis', 'Uninstall-Redis', 'Start-Redis')) {
        [IO.File]::WriteAllText((Join-Path $fixture "$name.ps1"), $stub)
    }
    foreach ($case in @(
        @('Install-Redis.bat', '', 'Install-Redis.ps1:False:en:True:False', 7),
        @('Update-Redis.bat', '', 'Update-Redis.ps1:False:en:True:False', 7),
        @('Uninstall-Redis.bat', '', 'Uninstall-Redis.ps1:False:en:True:False', 7),
        @('Purge-Redis.bat', 'N', '', 0),
        @('Purge-Redis.bat', 'Y', 'Uninstall-Redis.ps1:True:en:True:False', 7),
        @('Purge-Redis.bat --lang zh', 'N', '', 0),
        @('Purge-Redis.bat --lang zh', 'Y', 'Uninstall-Redis.ps1:True:zh:True:False', 7),
        @('Start-Redis.bat --lang zh', '', 'Start-Redis.ps1:False:zh:True:False', 7),
        @('Install-Redis.bat --lang zh --help', '', 'Install-Redis.ps1:False:zh:True:True', 7),
        @('Update-Redis.bat --help --lang zh', '', 'Update-Redis.ps1:False:zh:True:True', 7),
        @('Start-Redis.bat --lang invalid', '', '', 2),
        @('Uninstall-Redis.bat --lang', '', '', 2)
    )) {
        $marker = Join-Path $fixture 'called.txt'
        if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker }
        $start = New-Object Diagnostics.ProcessStartInfo
        $start.FileName = $env:ComSpec
        $start.Arguments = '/d /v:on /c ' + $case[0]
        $start.WorkingDirectory = $fixture
        $start.UseShellExecute = $false
        $start.RedirectStandardInput = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $process = [Diagnostics.Process]::Start($start)
        try {
            $process.StandardInput.WriteLine($case[1])
            $process.StandardInput.WriteLine()
            $process.StandardInput.Close()
            if (-not $process.WaitForExit(30000)) {
                $process.Kill()
                throw "Batch timed out: $($case[0])"
            }
            if ($process.ExitCode -ne $case[3]) {
                throw "Wrong exit code for $($case[0]): $($process.ExitCode); $($process.StandardError.ReadToEnd())"
            }
            $actual = ''
            if (Test-Path -LiteralPath $marker) { $actual = [IO.File]::ReadAllText($marker) }
            if ($actual -cne $case[2]) { throw "Wrong PowerShell dispatch for $($case[0]): $actual" }
        } finally { $process.Dispose() }
    }
} finally {
    Remove-Item -LiteralPath $fixture -Recurse -Force
}
Write-Host 'Windows batch entry point tests passed (routing, Unicode paths, exit codes, purge confirmation).'
