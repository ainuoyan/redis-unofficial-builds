Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:RedisPrefix = 'C:\Program Files\Redis-Unofficial'
$script:RedisServiceName = 'RedisUnofficial'
$script:RedisStateFile = Join-Path $script:RedisPrefix '.redis-package-state.json'
$script:RedisBackupRoot = 'C:\ProgramData\Redis-Unofficial\Backups'
$script:LifecycleMutex = $null

function Write-RedisInfo {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host "[redis-package] $Message"
}

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'This operation requires an elevated Administrator PowerShell session.'
    }
}

function Enter-RedisLifecycleLock {
    $script:LifecycleMutex = New-Object -TypeName Threading.Mutex `
        -ArgumentList @($false, 'Global\RedisUnofficialLifecycle')
    try {
        if (-not $script:LifecycleMutex.WaitOne(0, $false)) {
            throw 'Another Redis lifecycle operation is running.'
        }
    } catch [Threading.AbandonedMutexException] {
        # Ownership transfers to this process when the previous holder exited.
    }
}

function Exit-RedisLifecycleLock {
    if ($null -ne $script:LifecycleMutex) {
        try { $script:LifecycleMutex.ReleaseMutex() } catch { }
        $script:LifecycleMutex.Dispose()
        $script:LifecycleMutex = $null
    }
}

function Get-RedisPackageRoot {
    param([Parameter(Mandatory = $true)][string]$ScriptDirectory)
    $root = [IO.Path]::GetFullPath((Join-Path $ScriptDirectory '..'))
    if ([string]::Equals($root.TrimEnd('\'), $script:RedisPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The package scripts must be run from a separate extracted staging directory.'
    }
    Assert-NoReparsePoint -Path $root -Recurse
    return $root
}

function Assert-NoReparsePoint {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Recurse
    )
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Reparse points are not accepted: $Path"
    }
    if ($Recurse) {
        foreach ($child in Get-ChildItem -LiteralPath $Path -Force -Recurse -ErrorAction Stop) {
            if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Reparse points are not accepted: $($child.FullName)"
            }
        }
    }
}

function Read-PackageInfo {
    param([Parameter(Mandatory = $true)][string]$PackageRoot)
    $path = Join-Path $PackageRoot 'PACKAGE-INFO'
    if (-not [IO.File]::Exists($path)) { throw 'PACKAGE-INFO is missing.' }
    $values = @{}
    foreach ($line in [IO.File]::ReadAllLines($path, [Text.Encoding]::UTF8)) {
        if ($line -notmatch '^([A-Z][A-Z0-9_]*)=([^\x00-\x1f\x7f]*)$') {
            throw 'PACKAGE-INFO contains an invalid record.'
        }
        if ($values.ContainsKey($Matches[1])) { throw 'PACKAGE-INFO contains a duplicate key.' }
        $values[$Matches[1]] = $Matches[2]
    }
    $expected = @{
        PACKAGE_FORMAT = '3'
        PACKAGE_STATUS = 'experimental'
        PACKAGE_ID = 'redis-unofficial-builds'
        PACKAGE_VARIANT = 'windows-msys2'
        PACKAGE_ARCH = 'x64'
        OS = 'windows'
        RUNTIME = 'msys2'
        SERVICE_BACKEND = 'windows-scm'
        INSTALL_PREFIX = $script:RedisPrefix
    }
    foreach ($key in $expected.Keys) {
        if (-not $values.ContainsKey($key) -or $values[$key] -cne $expected[$key]) {
            throw "PACKAGE-INFO does not match the Windows MSYS2 contract: $key"
        }
    }
    if ($values['REDIS_VERSION'] -notmatch '^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$') {
        throw 'PACKAGE-INFO contains an invalid Redis version.'
    }
    return $values
}

function Test-RequiredPackageFiles {
    param([Parameter(Mandatory = $true)][string]$PackageRoot)
    $required = @(
        'bin\redis-server.exe', 'bin\redis-cli.exe', 'bin\redis-benchmark.exe',
        'bin\RedisService.exe', 'bin\msys-2.0.dll', 'conf\redis.conf',
        'conf\sentinel.conf', 'scripts\Common-Redis.ps1', 'scripts\Install-Redis.ps1',
        'scripts\Update-Redis.ps1', 'scripts\Uninstall-Redis.ps1',
        'MSYS2-RUNTIME-NOTICES.txt'
    )
    foreach ($relative in $required) {
        $path = Join-Path $PackageRoot $relative
        if (-not [IO.File]::Exists($path)) { throw "Package file is missing: $relative" }
        Assert-NoReparsePoint -Path $path
    }
}

function Assert-RedisPackageInventory {
    param([Parameter(Mandatory = $true)][string]$PackageRoot)
    $allowedDirectories = @('bin', 'conf', 'scripts')
    $allowedFiles = @(
        'bin\redis-server.exe', 'bin\redis-cli.exe', 'bin\redis-benchmark.exe',
        'bin\redis-check-aof.exe', 'bin\redis-check-rdb.exe', 'bin\redis-sentinel.exe',
        'bin\RedisService.exe', 'conf\redis.conf', 'conf\sentinel.conf',
        'scripts\Common-Redis.ps1', 'scripts\Install-Redis.ps1',
        'scripts\Update-Redis.ps1', 'scripts\Uninstall-Redis.ps1',
        'PACKAGE-INFO', 'BUILD-INFO', 'LICENSE.txt', 'README.txt',
        'THIRD_PARTY_NOTICES.md', 'UPSTREAM-CONTRIBUTOR-LICENSE.txt',
        'UPSTREAM-DEPENDENCY-NOTICES.txt', 'MSYS2-RUNTIME-NOTICES.txt'
    )
    $prefixLength = $PackageRoot.TrimEnd('\').Length + 1
    foreach ($item in Get-ChildItem -LiteralPath $PackageRoot -Force -Recurse -ErrorAction Stop) {
        $relative = $item.FullName.Substring($prefixLength)
        if ($item.PSIsContainer) {
            if ($allowedDirectories -inotcontains $relative) {
                throw "Package contains an unexpected directory: $relative"
            }
        } elseif ($allowedFiles -inotcontains $relative -and
            $relative -notmatch '^bin\\[A-Za-z0-9][A-Za-z0-9._+-]{0,126}\.dll$') {
            throw "Package contains an unexpected file: $relative"
        }
    }
}

function Test-RedisPackage {
    param([Parameter(Mandatory = $true)][string]$PackageRoot)
    if ($env:PROCESSOR_ARCHITECTURE -cne 'AMD64') {
        throw "The Windows MSYS2 package requires an x64 host; found $($env:PROCESSOR_ARCHITECTURE)."
    }
    Assert-NoReparsePoint -Path $PackageRoot -Recurse
    $info = Read-PackageInfo -PackageRoot $PackageRoot
    Test-RequiredPackageFiles -PackageRoot $PackageRoot
    Assert-RedisPackageInventory -PackageRoot $PackageRoot
    return $info
}

function Read-RedisState {
    if (-not [IO.File]::Exists($script:RedisStateFile)) { return $null }
    Assert-NoReparsePoint -Path $script:RedisStateFile
    $state = [IO.File]::ReadAllText($script:RedisStateFile, [Text.Encoding]::UTF8) | ConvertFrom-Json
    if ($state.StateFormat -ne 1 -or $state.PackageId -cne 'redis-unofficial-builds' -or
        $state.InstallPrefix -cne $script:RedisPrefix -or $state.PackageVariant -cne 'windows-msys2' -or
        $state.ServiceName -cne $script:RedisServiceName -or
        $state.RedisVersion -notmatch '^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$') {
        throw 'The existing Redis installation state is invalid.'
    }
    return $state
}

function Write-RedisState {
    param([Parameter(Mandatory = $true)][string]$Version)
    $state = [ordered]@{
        StateFormat = 1
        PackageId = 'redis-unofficial-builds'
        PackageStatus = 'experimental'
        InstallPrefix = $script:RedisPrefix
        RedisVersion = $Version
        PackageVariant = 'windows-msys2'
        ServiceName = $script:RedisServiceName
    }
    $temporary = "$script:RedisStateFile.tmp.$PID"
    [IO.File]::WriteAllText($temporary, ($state | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination $script:RedisStateFile -Force
    & icacls.exe $script:RedisStateFile /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to secure the installation state file.' }
}

function Write-ManagedRedisConfig {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    [IO.File]::Copy($Source, $Destination, $false)
    $managed = @'

# Managed experimental Windows defaults. Later records override upstream defaults.
bind 127.0.0.1
protected-mode yes
port 6379
daemonize no
supervised no
dir "data"
logfile "../log/redis.log"
pidfile "../run/redis.pid"
'@
    [IO.File]::AppendAllText($Destination, $managed, (New-Object Text.UTF8Encoding($false)))
}

function Copy-RedisProgramFiles {
    param([Parameter(Mandatory = $true)][string]$PackageRoot)
    foreach ($directory in @('bin', 'scripts')) {
        $destination = Join-Path $script:RedisPrefix $directory
        if ([IO.Directory]::Exists($destination)) { Remove-Item -LiteralPath $destination -Recurse -Force }
        Copy-Item -LiteralPath (Join-Path $PackageRoot $directory) -Destination $destination -Recurse
    }
    foreach ($name in @('PACKAGE-INFO', 'BUILD-INFO', 'LICENSE.txt', 'README.txt',
            'THIRD_PARTY_NOTICES.md', 'UPSTREAM-DEPENDENCY-NOTICES.txt',
            'MSYS2-RUNTIME-NOTICES.txt')) {
        Copy-Item -LiteralPath (Join-Path $PackageRoot $name) -Destination (Join-Path $script:RedisPrefix $name) -Force
    }
    $contributor = Join-Path $PackageRoot 'UPSTREAM-CONTRIBUTOR-LICENSE.txt'
    $installedContributor = Join-Path $script:RedisPrefix 'UPSTREAM-CONTRIBUTOR-LICENSE.txt'
    if ([IO.File]::Exists($contributor)) {
        Copy-Item -LiteralPath $contributor -Destination $installedContributor -Force
    } elseif ([IO.File]::Exists($installedContributor)) {
        Remove-Item -LiteralPath $installedContributor -Force
    }
}

function Write-RedisServiceSettings {
    $settings = [ordered]@{
        ConfigPath = (Join-Path $script:RedisPrefix 'conf\redis.conf')
        BindAddress = '127.0.0.1'
        Port = 6379
        ShutdownTimeoutSeconds = 60
    }
    $path = Join-Path $script:RedisPrefix 'RedisService.json'
    [IO.File]::WriteAllText($path, ($settings | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
}

function Set-RedisAccessControl {
    & icacls.exe $script:RedisPrefix /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-19:(OI)(CI)RX' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to secure the Redis installation prefix.' }
    foreach ($directory in @('data', 'log', 'run')) {
        $path = Join-Path $script:RedisPrefix $directory
        & icacls.exe $path /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-19:(OI)(CI)M' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Unable to secure $path." }
    }
}

function Get-RedisService {
    return Get-Service -Name $script:RedisServiceName -ErrorAction SilentlyContinue
}

function New-RedisService {
    if ($null -ne (Get-RedisService)) { throw 'RedisUnofficial service already exists.' }
    $wrapper = Join-Path $script:RedisPrefix 'bin\RedisService.exe'
    $binaryPath = '"' + $wrapper + '" --service'
    New-Service -Name $script:RedisServiceName -BinaryPathName $binaryPath -DisplayName 'Redis Unofficial (experimental)' `
        -Description 'Experimental MSYS2 Redis package from redis-unofficial-builds' -StartupType Automatic | Out-Null
    & sc.exe config $script:RedisServiceName start= delayed-auto | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to configure delayed service start.' }
    & sc.exe failure $script:RedisServiceName reset= 86400 actions= restart/5000/restart/15000/none/0 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to configure service recovery.' }
}

function Remove-RedisService {
    $service = Get-RedisService
    if ($null -eq $service) { return }
    try {
        if ($service.Status -ne [ServiceProcess.ServiceControllerStatus]::Stopped) {
            Stop-Service -Name $script:RedisServiceName -ErrorAction Stop
            $service.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Stopped, [TimeSpan]::FromSeconds(90))
        }
        & sc.exe delete $script:RedisServiceName | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Unable to delete the RedisUnofficial service.' }
    } finally {
        $service.Dispose()
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        $remaining = Get-RedisService
        if ($null -eq $remaining) { return }
        $remaining.Dispose()
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'RedisUnofficial service remained marked for deletion.'
}

function Start-RedisServiceAndWait {
    try {
        Start-Service -Name $script:RedisServiceName
    } catch {
        Write-RedisInfo 'Redis service startup failed; collecting diagnostics before rollback.'
        & sc.exe queryex $script:RedisServiceName
        foreach ($log in @(
                (Join-Path $script:RedisPrefix 'log\service-wrapper.log'),
                (Join-Path $script:RedisPrefix 'log\redis.log'))) {
            if (Test-Path -LiteralPath $log -PathType Leaf) {
                Write-Host "===== $log ====="
                Get-Content -LiteralPath $log -Tail 200
            }
        }
        throw
    }
    $service = Get-Service -Name $script:RedisServiceName
    $service.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Running, [TimeSpan]::FromSeconds(90))
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        $response = & (Join-Path $script:RedisPrefix 'bin\redis-cli.exe') -h 127.0.0.1 -p 6379 ping 2>$null
        if ($LASTEXITCODE -eq 0 -and ([string]$response).Trim() -ceq 'PONG') { return }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'RedisUnofficial service did not pass the Redis PING readiness check.'
}

function Stop-RedisServiceIfRunning {
    $service = Get-RedisService
    if ($null -ne $service -and $service.Status -ne [ServiceProcess.ServiceControllerStatus]::Stopped) {
        Stop-Service -Name $script:RedisServiceName -ErrorAction Stop
        $service.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Stopped, [TimeSpan]::FromSeconds(90))
    }
}
