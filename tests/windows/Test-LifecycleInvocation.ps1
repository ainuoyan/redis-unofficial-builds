param([string]$WorkflowPath = (Join-Path $PSScriptRoot '../../.github/workflows/build-experimental.yml'))

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
# Exercise the actual CI adapter with harmless scripts, never a real service.
$workflow = [IO.File]::ReadAllText($WorkflowPath)
$match = [regex]::Match($workflow, '(?ms)^          function Invoke-RedisLifecycleScript \{.*?^          \}')
if (-not $match.Success) { throw 'Missing lifecycle invocation adapter.' }
. ([scriptblock]::Create($match.Value))
$scripts = Join-Path ([IO.Path]::GetTempPath()) ('redis-invocation-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $scripts | Out-Null
try {
    $entry = Join-Path $scripts 'Install-Redis.ps1'
    foreach ($code in @(0, 1, 7)) {
        [IO.File]::WriteAllText($entry, "exit $code")
        $rejected = $false
        try { Invoke-RedisLifecycleScript -Name Install } catch { $rejected = $true }
        if ($rejected -ne ($code -ne 0)) { throw "Incorrect handling of exit $code." }
    }
    [IO.File]::WriteAllText($entry, 'throw "fixture exception"')
    $rejected = $false
    try { Invoke-RedisLifecycleScript -Name Install } catch { $rejected = $true }
    if (-not $rejected) { throw 'Script exceptions must propagate.' }
    $uninstall = Join-Path $scripts 'Uninstall-Redis.ps1'
    [IO.File]::WriteAllText($uninstall, 'param([switch]$Purge); if (-not $Purge) { exit 8 }; exit 0')
    Invoke-RedisLifecycleScript -Name Uninstall -Purge
    $rejected = $false
    try { Invoke-RedisLifecycleScript -Name Uninstall } catch { $rejected = $true }
    if (-not $rejected) { throw 'Purge switch was unexpectedly supplied.' }
} finally {
    $root = Get-Item -LiteralPath $scripts
    $files = @(Get-ChildItem -LiteralPath $scripts -Force)
    if (-not $root.PSIsContainer -or ($root.Attributes -band 1024) -or $files.Count -gt 2) {
        throw 'Unexpected invocation fixture cleanup scope.'
    }
    foreach ($file in $files) {
        if ($file.PSIsContainer -or ($file.Attributes -band 1024) -or
            $file.Name -cnotin @('Install-Redis.ps1', 'Uninstall-Redis.ps1')) {
            throw 'Unexpected invocation fixture file.'
        }
    }
    foreach ($file in $files) { Remove-Item -LiteralPath $file.FullName -Force }
    Remove-Item -LiteralPath $root.FullName
}
Write-Host 'Lifecycle invocation tests passed: zero/nonzero exit, exception, purge forwarding.'
# GitHub's PowerShell wrapper returns LASTEXITCODE. Expected failure probes (or
# an earlier test) must not leave a stale failure after all assertions passed.
exit 0
