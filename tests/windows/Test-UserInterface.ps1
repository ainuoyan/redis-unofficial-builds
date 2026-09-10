param([string]$ScriptsPath = (Join-Path $PSScriptRoot '../../packaging/windows/scripts'))

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
if ($env:OS -cne 'Windows_NT') {
    Write-Host 'Windows user interface tests skipped on a non-Windows host.'
    return
}

function Invoke-Entry {
    param([string]$Path, [string]$Arguments)
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $start.Arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $Path + '" ' + $Arguments
    $start.UseShellExecute = $false
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = [Text.UTF8Encoding]::new($false, $true)
    $start.StandardErrorEncoding = [Text.UTF8Encoding]::new($false, $true)
    $process = [Diagnostics.Process]::Start($start)
    try {
        $process.StandardInput.Close()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(30000)) {
            $process.Kill()
            throw "Entry timed out: $Path"
        }
        return @{ Code = $process.ExitCode; Output = $stdout.Result; Error = $stderr.Result }
    } finally { $process.Dispose() }
}

foreach ($file in Get-ChildItem -LiteralPath $ScriptsPath -Filter '*.ps1') {
    $tokens = $null
    $parseErrors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count -ne 0) { throw "PowerShell parse failed: $($file.Name): $parseErrors" }
    $bytes = [IO.File]::ReadAllBytes($file.FullName)
    if ($bytes.Length -lt 3 -or $bytes[0] -ne 239 -or $bytes[1] -ne 187 -or $bytes[2] -ne 191) {
        throw "Windows PowerShell UTF-8 BOM missing: $($file.Name)"
    }
}

$originalEncoding = [Console]::OutputEncoding
try {
    foreach ($codePage in @(437, 936, 65001)) {
        [Console]::OutputEncoding = [Text.Encoding]::GetEncoding($codePage)
        foreach ($name in @('Install', 'Update', 'Uninstall', 'Start')) {
            $path = Join-Path $ScriptsPath "$name-Redis.ps1"
            $english = Invoke-Entry $path '-Help'
            $chinese = Invoke-Entry $path '-Help -Lang zh'
            if ($english.Code -ne 0 -or $english.Output -notmatch 'Usage:') {
                throw "English help failed: $name, code page $codePage"
            }
            if ($english.Output -match '[\u4e00-\u9fff]') { throw "Default output is not English: $name" }
            if ($chinese.Code -ne 0 -or $chinese.Output -notmatch '用法：') {
                throw "Chinese UTF-8 help failed: $name, code page $codePage; $($chinese.Error)"
            }
            # Also check restoration in the calling PowerShell session, including
            # the early return used by help. Do not run any lifecycle operation.
            & $path -Help -Lang zh 6>&1 | Out-Null
            if ([Console]::OutputEncoding.CodePage -ne $codePage) {
                throw "Entry did not restore console encoding: $name"
            }
        }
    }
} finally { [Console]::OutputEncoding = $originalEncoding }

$fixture = Join-Path $env:TEMP ('Redis UI 中文 % ' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $fixture | Out-Null
try {
    foreach ($name in @('bin', 'conf', 'scripts')) {
        New-Item -ItemType Directory -Path (Join-Path $fixture $name) | Out-Null
    }
    $entry = Join-Path $fixture 'scripts\Start-Redis.ps1'
    Copy-Item -LiteralPath (Join-Path $ScriptsPath 'Start-Redis.ps1') -Destination $entry
    $config = Join-Path $fixture 'conf\redis.conf'
    [IO.File]::WriteAllText($config, '# Existing configuration: 中文', [Text.UTF8Encoding]::new($false))
    $before = [IO.File]::ReadAllText($config)
    $probeSource = @'
using System;
using System.IO;
using System.Text;
public class RedisUiLaunchProbe {
    public static int Main(string[] args) {
        File.WriteAllLines("invocation.txt", new string[] {
            Environment.CurrentDirectory, args.Length.ToString(), args[0]
        }, new UTF8Encoding(false));
        return 7;
    }
}
'@
    Add-Type -TypeDefinition $probeSource -OutputAssembly (Join-Path $fixture 'bin\redis-server.exe') -OutputType ConsoleApplication
    $result = Invoke-Entry $entry '-Lang zh'
    if ($result.Code -ne 7 -or $result.Output -notmatch '正在使用') {
        throw "Direct start failed to preserve the Redis exit code or Chinese output: $($result.Error)"
    }
    $englishStart = Invoke-Entry $entry ''
    if ($englishStart.Code -ne 7 -or -not $englishStart.Output.Contains($config)) {
        throw 'Default English output lost the Chinese configuration path.'
    }
    $actual = [IO.File]::ReadAllLines((Join-Path $fixture 'invocation.txt'))
    if ($actual.Count -ne 3 -or $actual[0] -cne $fixture -or $actual[1] -cne '1' -or $actual[2] -cne 'conf/redis.conf') {
        throw 'Direct start changed the working directory contract or configuration arguments.'
    }
    if ([IO.File]::ReadAllText($config) -cne $before -or (Test-Path -LiteralPath (Join-Path $fixture 'portable'))) {
        throw 'Direct start rewrote configuration or created a portable directory.'
    }
    Move-Item -LiteralPath $config -Destination ($config + '.saved')
    $missing = Invoke-Entry $entry '-Lang zh'
    if ($missing.Code -ne 1 -or $missing.Error -notmatch '缺少默认配置文件') {
        throw "Missing default configuration did not produce a localized error. Code=$($missing.Code); stdout=$($missing.Output); stderr=$($missing.Error)"
    }
    $invalid = Invoke-Entry $entry '-Lang invalid'
    if ($invalid.Code -ne 1 -or $invalid.Error -notmatch 'Invalid language') {
        throw 'Invalid language was not rejected before launching Redis.'
    }
} finally {
    $root = Get-Item -LiteralPath $fixture -Force
    $items = @(Get-ChildItem -LiteralPath $fixture -Force -Recurse)
    foreach ($item in @($root) + $items) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing fixture cleanup through a reparse point: $($item.FullName)"
        }
    }
    if (-not $root.PSIsContainer -or $items.Count -gt 20) { throw 'Unexpected fixture cleanup scope.' }
    Remove-Item -LiteralPath $fixture -Recurse -Force
}
Write-Host 'Windows UI tests passed: PowerShell 5.1, code pages 437/936/65001, UTF-8 Chinese, encoding restoration, Unicode paths, direct-start arguments and exit codes.'
