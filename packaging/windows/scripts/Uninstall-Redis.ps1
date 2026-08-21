param([switch]$Purge)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common-Redis.ps1')

Assert-Administrator
Enter-RedisLifecycleLock
try {
    $state = Read-RedisState
    if ($null -eq $state) {
        if ($null -eq (Get-RedisService) -and -not [IO.Directory]::Exists($script:RedisPrefix)) {
            Write-RedisInfo 'RedisUnofficial is already uninstalled.'
            return
        }
        throw 'Refusing to remove an installation without valid managed state.'
    }
    Assert-NoReparsePoint -Path $script:RedisPrefix -Recurse
    Stop-RedisServiceIfRunning
    Remove-RedisService
    if ($Purge) {
        Remove-Item -LiteralPath $script:RedisPrefix -Recurse -Force
        Write-RedisInfo 'Removed Redis program, configuration, data, and logs.'
    } else {
        foreach ($name in @('bin', 'scripts', 'PACKAGE-INFO', 'BUILD-INFO', 'LICENSE.txt', 'README.txt',
                'THIRD_PARTY_NOTICES.md', 'UPSTREAM-CONTRIBUTOR-LICENSE.txt',
                'UPSTREAM-DEPENDENCY-NOTICES.txt', 'MSYS2-RUNTIME-NOTICES.txt',
                'RedisService.json')) {
            $path = Join-Path $script:RedisPrefix $name
            if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
        }
        Write-RedisInfo 'Removed Redis program and service; conf, data, logs, and state were preserved.'
    }
} finally {
    Exit-RedisLifecycleLock
}
