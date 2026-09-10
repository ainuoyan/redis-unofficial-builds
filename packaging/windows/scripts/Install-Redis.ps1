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
        [Security.AccessControl.FileSystemRights]::Modify -bor
        [Security.AccessControl.FileSystemRights]::FullControl -bor
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
    Assert-RedisPortAvailable

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
        Set-RedisAccessControl
        & (Join-Path $script:RedisPrefix 'bin\RedisService.exe') --self-test
        if ($LASTEXITCODE -ne 0) { throw 'RedisService self-test failed.' }
        Write-RedisState -Version $info['REDIS_VERSION'] -PackageStatus $info['PACKAGE_STATUS']
        New-RedisService
        Start-RedisServiceAndWait
        $installed = $true
    } finally {
        if (-not $installed) {
            Remove-RedisService
            if ([IO.Directory]::Exists($script:RedisPrefix)) {
                Assert-NoReparsePoint -Path $script:RedisPrefix
                Remove-Item -LiteralPath $script:RedisPrefix -Recurse -Force
            }
        }
    }
    Write-RedisInfo "Installed Redis $($info['REDIS_VERSION']) as the RedisUnofficial service."
} finally {
    Exit-RedisLifecycleLock
}
