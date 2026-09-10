param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$bootstrapTrustedOwnerSids = @(
    'S-1-5-18',
    'S-1-5-32-544',
    'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
)

function Assert-RedisBootstrapAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Ancestor
    )
    $item = Microsoft.PowerShell.Management\Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Lifecycle staging paths must not contain reparse points: $Path"
    }
    $acl = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $Path -ErrorAction Stop
    $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($bootstrapTrustedOwnerSids -notcontains $ownerSid) {
        throw "Lifecycle scripts must be run from an Administrator-controlled staging tree: $Path"
    }
    $writeMask = [Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    if ($Ancestor) {
        $writeMask = [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
            [Security.AccessControl.FileSystemRights]::Delete -bor
            [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
            [Security.AccessControl.FileSystemRights]::TakeOwnership
    }
    foreach ($rule in $acl.GetAccessRules(
            $true, $true, [Security.Principal.SecurityIdentifier])) {
        if (($rule.PropagationFlags -band
                [Security.AccessControl.PropagationFlags]::InheritOnly) -eq 0 -and
            $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
            $bootstrapTrustedOwnerSids -notcontains $rule.IdentityReference.Value -and
            ([int64]$rule.FileSystemRights -band [int64]$writeMask) -ne 0) {
            throw "Lifecycle staging path grants unsafe access: $Path"
        }
    }
}

$bootstrapIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$bootstrapPrincipal = [Security.Principal.WindowsPrincipal]::new($bootstrapIdentity)
if (-not $bootstrapPrincipal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'This operation requires an elevated Administrator PowerShell session.'
}
$bootstrapPackageRoot = [IO.Path]::GetFullPath([IO.Path]::Combine($PSScriptRoot, '..'))
$bootstrapCurrent = [IO.DirectoryInfo]::new($bootstrapPackageRoot)
while ($null -ne $bootstrapCurrent) {
    Assert-RedisBootstrapAcl -Path $bootstrapCurrent.FullName -Ancestor
    $bootstrapCurrent = $bootstrapCurrent.Parent
}
foreach ($bootstrapPath in @(
        $bootstrapPackageRoot,
        $PSScriptRoot,
        $PSCommandPath,
        [IO.Path]::Combine($PSScriptRoot, 'Common-Redis.ps1'))) {
    Assert-RedisBootstrapAcl -Path $bootstrapPath
}

. ([IO.Path]::Combine($PSScriptRoot, 'Common-Redis.ps1'))

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

    $backup = New-RedisBackupDirectory -Version $state.RedisVersion
    foreach ($name in @('bin', 'scripts', 'PACKAGE-INFO', 'BUILD-INFO', 'LICENSE.txt', 'README.txt',
            'THIRD_PARTY_NOTICES.md', 'UPSTREAM-CONTRIBUTOR-LICENSE.txt',
            'UPSTREAM-DEPENDENCY-NOTICES.txt', 'MSYS2-RUNTIME-NOTICES.txt',
            'RedisService.json', '.redis-package-state.json')) {
        $source = Join-Path $script:RedisPrefix $name
        if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $backup -Recurse }
    }
    Set-RedisAdministrativeTreeAcl -Path $backup
    Assert-RedisBackupDirectory -Path $backup
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
            Assert-RedisBackupDirectory -Path $backup
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
