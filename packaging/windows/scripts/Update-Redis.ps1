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
        throw 'Downgrades require a separate data-compatibility migration and are not supported by this updater.'
    }
    $service = Get-RedisService
    # Refresh all managed files even when the Redis version/wrapper are unchanged.
    $oldAccount = $null
    if ($null -ne $service) { $oldAccount = Get-RedisServiceAccount }

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
        Copy-RedisProgramFiles -PackageRoot $packageRoot
        Set-RedisAccessControl
        & (Join-Path $script:RedisPrefix 'bin\RedisService.exe') --self-test
        if ($LASTEXITCODE -ne 0) { throw 'RedisService self-test failed.' }
        # Legacy settings are backed up above for rollback, but the new wrapper
        # reads conf\redis.conf exclusively. Never modify the user's conf/data.
        $legacySettings = Join-Path $script:RedisPrefix 'RedisService.json'
        if ([IO.File]::Exists($legacySettings)) { Remove-Item -LiteralPath $legacySettings }
        Write-RedisState -Version $info['REDIS_VERSION'] -PackageStatus $info['PACKAGE_STATUS']
        if ($serviceWasPresent) {
            Set-RedisServiceAccount -Account 'NT AUTHORITY\LocalService'
            Set-RedisServiceRecovery
        } else {
            New-RedisService
        }
        if ($wasRunning -or -not $serviceWasPresent) { Start-RedisServiceAndWait }
        $updated = $true
    } finally {
        if (-not $updated) {
            # Never replace files beneath a child that could not be stopped.
            Stop-RedisServiceIfRunning
            if (-not $serviceWasPresent) { Remove-RedisService }
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
                Set-RedisServiceAccount -Account $oldAccount
                Set-RedisServiceRecovery
                if ($wasRunning) { Start-RedisServiceAndWait }
            }
        }
    }
    Write-RedisInfo "Updated Redis from $($state.RedisVersion) to $($info['REDIS_VERSION']); conf and data were preserved."
} finally {
    Exit-RedisLifecycleLock
}
