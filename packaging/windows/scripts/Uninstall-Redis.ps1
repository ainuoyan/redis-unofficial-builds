param([switch]$Purge, [switch]$ConfirmPurge, [string]$Lang = 'en', [switch]$Help, [switch]$FromBatch)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$script:RedisUiLanguage = 'en'
$redisExitCode = 0
$redisOriginalEncoding = $null

function Get-RedisText {
    param([string]$English, [string]$Chinese)
    if ($script:RedisUiLanguage -eq 'zh') { return $Chinese }
    return $English
}

try {
    if ($Lang -notin @('en', 'zh')) {
        throw 'Invalid language. Use -Lang en or -Lang zh.'
    }
    $script:RedisUiLanguage = $Lang
    # UTF-8 source BOM supports Windows PowerShell 5.1. Console encoding is
    # temporary; BAT contains ASCII only and never changes the host code page.
    # English messages can also contain a Chinese file path.
    try {
        $encodingBeforeChange = [Console]::OutputEncoding
        [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
        $redisOriginalEncoding = $encodingBeforeChange
    } catch {
        $script:RedisUiLanguage = 'en'
        Write-Host 'UTF-8 console output is unavailable; using English. Use -Lang en if text is unreadable.'
    }
    if ($Help) {
        Write-Host (Get-RedisText 'Usage: Uninstall-Redis.ps1 [-Purge] [-Lang en|zh]' '用法：Uninstall-Redis.ps1 [-Purge] [-Lang en|zh]')
        Write-Host (Get-RedisText 'Run as Administrator. English is the default. Use -Lang zh for Chinese.' '请以管理员身份运行。默认英文；使用 -Lang zh 切换中文，显示异常时使用 -Lang en。')
        return
    }

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
            throw (Get-RedisText "Lifecycle staging paths must not contain reparse points: $Path" "生命周期操作的暂存路径不能包含重解析点：$Path")
        }
        $acl = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $Path -ErrorAction Stop
        $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($bootstrapTrustedOwnerSids -notcontains $ownerSid) {
            throw (Get-RedisText "Lifecycle scripts must be run from an Administrator-controlled staging tree: $Path" "生命周期脚本必须从管理员控制的暂存目录运行：$Path")
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
                throw (Get-RedisText "Lifecycle staging path grants unsafe access: $Path" "生命周期操作的暂存路径授予了不安全的访问权限：$Path")
            }
        }
    }

    $bootstrapIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $bootstrapPrincipal = [Security.Principal.WindowsPrincipal]::new($bootstrapIdentity)
    if (-not $bootstrapPrincipal.IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw (Get-RedisText 'This operation requires an elevated Administrator PowerShell session.' "此操作需要以管理员身份运行 PowerShell。")
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

    if ($Purge -and $ConfirmPurge) {
        $answer = Read-Host (Get-RedisText 'This removes Redis programs, configuration, data and logs. Continue? [Y/N]' '此操作会删除 Redis 程序、配置、数据和日志。是否继续？[Y/N]')
        if ($answer -ine 'Y') {
            Write-RedisInfo (Get-RedisText 'Cancelled; no changes were made.' '已取消；未做任何更改。')
            return
        }
    }
    Assert-Administrator
    Enter-RedisLifecycleLock
    try {
        $state = Read-RedisState
        if ($null -eq $state) {
            if ($null -eq (Get-RedisService) -and -not [IO.Directory]::Exists($script:RedisPrefix)) {
                Write-RedisInfo (Get-RedisText 'RedisUnofficial is already uninstalled.' "RedisUnofficial 已卸载。")
                return
            }
            throw (Get-RedisText 'Refusing to remove an installation without valid managed state.' "拒绝删除没有有效受管理状态的安装。")
        }
        Assert-NoReparsePoint -Path $script:RedisPrefix -Recurse
        Stop-RedisServiceIfRunning
        Remove-RedisService
        if ($Purge) {
            Remove-Item -LiteralPath $script:RedisPrefix -Recurse -Force
            Write-RedisInfo (Get-RedisText 'Removed Redis program, configuration, data, and logs.' "已删除 Redis 程序、配置、数据和日志。")
        } else {
            foreach ($name in @('bin', 'scripts', 'PACKAGE-INFO', 'BUILD-INFO', 'LICENSE.txt', 'README.txt',
                    'THIRD_PARTY_NOTICES.md', 'UPSTREAM-CONTRIBUTOR-LICENSE.txt',
                    'UPSTREAM-DEPENDENCY-NOTICES.txt', 'MSYS2-RUNTIME-NOTICES.txt',
                    'RedisService.json')) {
                $path = Join-Path $script:RedisPrefix $name
                if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
            }
            Write-RedisInfo (Get-RedisText 'Removed Redis program and service; conf, data, logs, and state were preserved.' "已删除 Redis 程序和服务；配置、数据、日志和状态已保留。")
        }
    } finally {
        Exit-RedisLifecycleLock
    }
} catch {
    $redisExitCode = 1
    # Avoid host-dependent Write-Error wrapping/serialization of CJK messages.
    [Console]::Error.WriteLine(('[redis-package] {0}: {1}' -f
        (Get-RedisText 'ERROR' '错误'), $_.Exception.Message))
} finally {
    try {
        if ($FromBatch -and -not [Console]::IsInputRedirected) {
            [void](Read-Host (Get-RedisText 'Press Enter to close' '按回车键关闭'))
        }
    } finally {
        if ($null -ne $redisOriginalEncoding) {
            [Console]::OutputEncoding = $redisOriginalEncoding
        }
    }
}
exit $redisExitCode
