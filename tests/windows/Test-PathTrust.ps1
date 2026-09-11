param([string]$CommonPath = (Join-Path $PSScriptRoot '../../packaging/windows/scripts/Common-Redis.ps1'))

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

if ($env:OS -cne 'Windows_NT') {
    Write-Host 'Windows path trust tests skipped on a non-Windows host.'
    return
}

. $CommonPath

function Assert-Throws {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Action,
        [Parameter(Mandatory = $true)][string]$Message
    )
    try {
        & $Action
    } catch {
        return
    }
    throw $Message
}

$stage = Join-Path $env:ProgramFiles "Redis-Unofficial-Trust-Test-$([Guid]::NewGuid().ToString('N'))"
try {
    New-Item -ItemType Directory -Path $stage -ErrorAction Stop | Out-Null
    Set-RedisAdministrativeAcl -Path $stage
    $file = Join-Path $stage 'payload.txt'
    [IO.File]::WriteAllText($file, 'trusted', [Text.Encoding]::UTF8)
    & icacls.exe $stage /setowner '*S-1-5-32-544' /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to prepare the trusted owner fixture.' }
    Assert-RedisTrustedTree -Path $stage

    Set-RedisAdministrativeTreeAcl -Path $stage
    Assert-RedisTrustedTree -Path $stage
    if ([IO.File]::ReadAllText($file, [Text.Encoding]::UTF8) -cne 'trusted') {
        throw 'Administrative tree protection made the backup unreadable.'
    }

    & icacls.exe $file /grant '*S-1-5-19:RX' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to prepare the read-only service fixture.' }
    Assert-RedisTrustedTree -Path $stage

    & icacls.exe $file /grant '*S-1-5-32-545:M' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to prepare the untrusted ACL fixture.' }
    Assert-Throws -Action { Assert-RedisTrustedTree -Path $stage } `
        -Message 'An untrusted writable ACL was accepted.'
} finally {
    if ([IO.Directory]::Exists($stage)) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}

Write-Host 'Windows path trust tests passed.'
