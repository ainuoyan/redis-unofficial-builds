param([switch]$Purge)

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
