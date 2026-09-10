param([string]$Lang = 'en', [switch]$Help, [switch]$FromBatch)

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
        Write-Host (Get-RedisText 'Usage: Install-Redis.ps1 [-Lang en|zh]' '用法：Install-Redis.ps1 [-Lang en|zh]')
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
                Write-RedisInfo (Get-RedisText "Redis $($info['REDIS_VERSION']) is already installed; no changes were made." "Redis $($info['REDIS_VERSION']) 已安装；未做任何更改。")
                return
            }
            throw (Get-RedisText 'A managed installation already exists; use Update-Redis.ps1.' "已存在本项目管理的安装；请使用 Update-Redis.ps1。")
        }
        if ([IO.Directory]::Exists($script:RedisPrefix) -or [IO.File]::Exists($script:RedisPrefix) -or $null -ne (Get-RedisService)) {
            throw (Get-RedisText 'Refusing to overwrite an existing path or service.' "拒绝覆盖现有路径或服务。")
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
            if ($LASTEXITCODE -ne 0) { throw (Get-RedisText 'RedisService self-test failed.' "RedisService 自检失败。") }
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
        Write-RedisInfo (Get-RedisText "Installed Redis $($info['REDIS_VERSION']) as the RedisUnofficial service." "已将 Redis $($info['REDIS_VERSION']) 安装为 RedisUnofficial 服务。")
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
