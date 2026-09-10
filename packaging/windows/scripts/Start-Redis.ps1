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
        Write-Host (Get-RedisText 'Usage: Start-Redis.ps1 [-Lang en|zh]' '用法：Start-Redis.ps1 [-Lang en|zh]')
        Write-Host (Get-RedisText 'Start Redis with the existing conf\redis.conf. No service is installed. English is the default.' '使用现有 conf\redis.conf 启动 Redis，不安装服务。默认英文；使用 -Lang zh 切换中文。')
        return
    }

    $packageRoot = [IO.Path]::GetFullPath([IO.Path]::Combine($PSScriptRoot, '..'))
    $server = [IO.Path]::Combine($packageRoot, 'bin', 'redis-server.exe')
    $config = [IO.Path]::Combine($packageRoot, 'conf', 'redis.conf')
    if (-not [IO.File]::Exists($server)) {
        throw (Get-RedisText "Redis executable is missing: $server" "缺少 Redis 程序：$server")
    }
    if (-not [IO.File]::Exists($config)) {
        throw (Get-RedisText "Default configuration is missing: $config" "缺少默认配置文件：$config")
    }
    Write-Host (Get-RedisText "Starting Redis with $config" "正在使用 $config 启动 Redis")
    Write-Host (Get-RedisText 'No service is installed. Data paths follow the configuration. Press Ctrl+C to stop.' '不安装服务。数据路径按配置执行。按 Ctrl+C 停止。')
    Push-Location -LiteralPath $packageRoot
    try {
        & $server $config
        $redisExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
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
