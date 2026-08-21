param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common-Redis.ps1')

Assert-Administrator
Enter-RedisLifecycleLock
try {
    $packageRoot = Get-RedisPackageRoot -ScriptDirectory $PSScriptRoot
    $info = Test-RedisPackage -PackageRoot $packageRoot
    $state = Read-RedisState
    if ($null -ne $state) {
        $service = Get-RedisService
        if ($state.RedisVersion -ceq $info['REDIS_VERSION'] -and $null -ne $service -and
            [IO.File]::Exists((Join-Path $script:RedisPrefix 'bin\redis-server.exe'))) {
            Write-RedisInfo "Redis $($info['REDIS_VERSION']) is already installed; no changes were made."
            return
        }
        throw 'A managed installation already exists; use Update-Redis.ps1.'
    }
    if ([IO.Directory]::Exists($script:RedisPrefix) -or [IO.File]::Exists($script:RedisPrefix) -or $null -ne (Get-RedisService)) {
        throw 'Refusing to overwrite an existing path or service.'
    }

    $installed = $false
    try {
        New-Item -ItemType Directory -Path $script:RedisPrefix | Out-Null
        foreach ($directory in @('conf', 'data', 'log', 'run')) {
            New-Item -ItemType Directory -Path (Join-Path $script:RedisPrefix $directory) | Out-Null
        }
        Write-ManagedRedisConfig -Source (Join-Path $packageRoot 'conf\redis.conf') `
            -Destination (Join-Path $script:RedisPrefix 'conf\redis.conf')
        Copy-Item -LiteralPath (Join-Path $packageRoot 'conf\sentinel.conf') `
            -Destination (Join-Path $script:RedisPrefix 'conf\sentinel.conf')
        Copy-RedisProgramFiles -PackageRoot $packageRoot
        Write-RedisServiceSettings
        Set-RedisAccessControl
        & (Join-Path $script:RedisPrefix 'bin\RedisService.exe') --self-test
        if ($LASTEXITCODE -ne 0) { throw 'RedisService self-test failed.' }
        Write-RedisState -Version $info['REDIS_VERSION']
        New-RedisService
        Start-RedisServiceAndWait
        $installed = $true
    } finally {
        if (-not $installed) {
            try { Remove-RedisService } catch { }
            if ([IO.Directory]::Exists($script:RedisPrefix)) {
                Assert-NoReparsePoint -Path $script:RedisPrefix
                Remove-Item -LiteralPath $script:RedisPrefix -Recurse -Force
            }
        }
    }
    Write-RedisInfo "Installed Redis $($info['REDIS_VERSION']) as the experimental RedisUnofficial service."
} finally {
    Exit-RedisLifecycleLock
}
