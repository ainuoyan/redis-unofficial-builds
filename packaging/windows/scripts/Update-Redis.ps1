param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common-Redis.ps1')

Assert-Administrator
Enter-RedisLifecycleLock
try {
    $state = Read-RedisState
    if ($null -eq $state) { throw 'No managed Redis installation was found.' }
    Assert-NoReparsePoint -Path $script:RedisPrefix
    $packageRoot = Get-RedisPackageRoot -ScriptDirectory $PSScriptRoot
    $info = Test-RedisPackage -PackageRoot $packageRoot
    if ([version]$info['REDIS_VERSION'] -lt [version]$state.RedisVersion) {
        throw 'Downgrades require a separate data-compatibility migration and are not supported by this experimental updater.'
    }
    $service = Get-RedisService
    if ($state.RedisVersion -ceq $info['REDIS_VERSION'] -and $null -ne $service -and
        [IO.File]::Exists((Join-Path $script:RedisPrefix 'bin\redis-server.exe'))) {
        $service.Dispose()
        Write-RedisInfo "Redis $($info['REDIS_VERSION']) is already installed; no changes were made."
        return
    }

    $timestamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
    $backup = Join-Path $script:RedisBackupRoot "$($state.RedisVersion)-$timestamp-$PID"
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    foreach ($name in @('bin', 'scripts', 'PACKAGE-INFO', 'BUILD-INFO', 'LICENSE.txt', 'README.txt',
            'THIRD_PARTY_NOTICES.md', 'UPSTREAM-CONTRIBUTOR-LICENSE.txt',
            'UPSTREAM-DEPENDENCY-NOTICES.txt', 'MSYS2-RUNTIME-NOTICES.txt',
            'RedisService.json', '.redis-package-state.json')) {
        $source = Join-Path $script:RedisPrefix $name
        if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $backup -Recurse }
    }
    $serviceWasPresent = $null -ne $service
    $wasRunning = $serviceWasPresent -and $service.Status -ne [ServiceProcess.ServiceControllerStatus]::Stopped
    if ($serviceWasPresent) {
        $service.Dispose()
        $service = $null
    }
    $updated = $false
    try {
        Stop-RedisServiceIfRunning
        if ($null -ne (Get-RedisService)) { Remove-RedisService }
        Copy-RedisProgramFiles -PackageRoot $packageRoot
        Write-RedisServiceSettings
        Set-RedisAccessControl
        & (Join-Path $script:RedisPrefix 'bin\RedisService.exe') --self-test
        if ($LASTEXITCODE -ne 0) { throw 'RedisService self-test failed.' }
        Write-RedisState -Version $info['REDIS_VERSION']
        New-RedisService
        if ($wasRunning -or -not $serviceWasPresent) { Start-RedisServiceAndWait }
        $updated = $true
    } finally {
        if (-not $updated) {
            try { Stop-RedisServiceIfRunning } catch { }
            try { Remove-RedisService } catch { }
            foreach ($name in @('bin', 'scripts', 'PACKAGE-INFO', 'BUILD-INFO', 'LICENSE.txt', 'README.txt',
                    'THIRD_PARTY_NOTICES.md', 'UPSTREAM-CONTRIBUTOR-LICENSE.txt',
                    'UPSTREAM-DEPENDENCY-NOTICES.txt', 'MSYS2-RUNTIME-NOTICES.txt',
                    'RedisService.json', '.redis-package-state.json')) {
                $target = Join-Path $script:RedisPrefix $name
                if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
                $saved = Join-Path $backup $name
                if (Test-Path -LiteralPath $saved) { Copy-Item -LiteralPath $saved -Destination $target -Recurse }
            }
            Set-RedisAccessControl
            if ($serviceWasPresent) {
                New-RedisService
                if ($wasRunning) { try { Start-RedisServiceAndWait } catch { } }
            }
        }
    }
    Write-RedisInfo "Updated Redis from $($state.RedisVersion) to $($info['REDIS_VERSION']); conf and data were preserved."
} finally {
    Exit-RedisLifecycleLock
}
