param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# Load the actual generator without executing Windows-specific lifecycle setup.
$commonPath = Join-Path $PSScriptRoot '../../packaging/windows/scripts/Common-Redis.ps1'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $commonPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) { throw ($parseErrors | Out-String) }
$definitions = @($ast.FindAll({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq 'Write-ManagedRedisConfig'
}, $false))
if ($definitions.Count -ne 1) { throw 'Expected one real configuration generator.' }
. ([scriptblock]::Create($definitions[0].Extent.Text))

$utf8 = New-Object Text.UTF8Encoding($false)
$defaults = @(
    'bind 127.0.0.1', 'protected-mode yes', 'port 6379', 'daemonize no',
    'supervised no', 'dir "data"', 'logfile "../log/redis.log"', 'pidfile "../run/redis.pid"'
)

function Assert-Equal {
    param($Expected, $Actual, [string]$Message)
    if ($Expected -cne $Actual) { throw $Message }
}

function New-TestConfig {
    param([string]$Directory, [AllowEmptyString()][string]$Contents)
    $source = Join-Path $Directory 'source.conf'
    $destination = Join-Path $Directory 'redis.conf'
    [IO.File]::WriteAllText($source, $Contents, $utf8)
    Write-ManagedRedisConfig -Source $source -Destination $destination
    Assert-Equal $Contents ([IO.File]::ReadAllText($source)) 'Source configuration was changed.'
    return $destination
}

function Assert-Defaults {
    param([string]$Path)
    $lines = [IO.File]::ReadAllLines($Path)
    foreach ($record in $defaults) {
        $key = $record.Split(' ')[0]
        $active = @($lines | Where-Object { $_ -match "^[ `t]*$key(?:[ `t]|$)" })
        Assert-Equal 1 $active.Count "Expected exactly one active $key directive."
        Assert-Equal $record $active[0] "Incorrect default for $key."
    }
}

$cases = [ordered]@{
    'defaults stay at their original locations' = {
        param($directory)
        $source = @(
            '# network', 'bind 0.0.0.0', 'protected-mode no', 'port 6380',
            '# process', 'daemonize yes', 'supervised auto',
            '# persistence', 'dir ./', 'logfile ""', 'pidfile /tmp/redis.pid', 'save 60 1'
        ) -join "`n"
        $path = New-TestConfig $directory $source
        $expected = @(
            '# network', $defaults[0], $defaults[1], $defaults[2],
            '# process', $defaults[3], $defaults[4],
            '# persistence', $defaults[5], $defaults[6], $defaults[7], 'save 60 1'
        ) -join "`n"
        Assert-Equal ($expected + "`n") ([IO.File]::ReadAllText($path).Replace("`r`n", "`n")) 'Defaults moved or unrelated lines changed.'
        Assert-Defaults $path
    }
    'editing the original port and bind is not overridden later' = {
        param($directory)
        $path = New-TestConfig $directory "port 6379`nbind 127.0.0.1`nsave 60 1`n"
        $lines = [IO.File]::ReadAllLines($path)
        # Reproduce editing the first settings in a text editor, not appending overrides.
        $lines[0] = 'port 16379'
        $lines[1] = 'bind 192.0.2.10'
        [IO.File]::WriteAllLines($path, $lines, $utf8)
        $ports = @([IO.File]::ReadAllLines($path) | Where-Object { $_ -match '^port ' })
        $binds = @([IO.File]::ReadAllLines($path) | Where-Object { $_ -match '^bind ' })
        Assert-Equal 1 $ports.Count 'A later port directive overrides the user edit.'
        Assert-Equal 'port 16379' $ports[0] 'User port was lost.'
        Assert-Equal 1 $binds.Count 'A later bind directive overrides the user edit.'
        Assert-Equal 'bind 192.0.2.10' $binds[0] 'User bind was lost.'
    }
    'dir precedes logfile even when upstream lists logfile first' = {
        param($directory)
        $path = New-TestConfig $directory "port 6379`n# logging`nlogfile `"`"`n# data`ndir ./`n"
        Assert-Defaults $path
        $lines = [IO.File]::ReadAllLines($path)
        Assert-Equal 'port 6379' $lines[0] 'Original port position moved.'
        Assert-Equal 'dir "data"' $lines[2] 'Data directory must be set before opening the logfile.'
        Assert-Equal 'logfile "../log/redis.log"' $lines[3] 'Log path lost its data-relative location.'
    }
    'missing dir is inserted before an existing logfile' = {
        param($directory)
        $path = New-TestConfig $directory 'logfile ""'
        Assert-Defaults $path
        $lines = [IO.File]::ReadAllLines($path)
        Assert-Equal 'dir "data"' $lines[0] 'Missing dir was added after logfile.'
        Assert-Equal 'logfile "../log/redis.log"' $lines[1] 'Unexpected log path.'
    }
    'duplicate defaults including mixed case and tabs are removed' = {
        param($directory)
        $source = (($defaults + ($defaults | ForEach-Object { "`t" + $_.ToUpperInvariant().Replace(' ', "`t") })) -join "`n")
        $path = New-TestConfig $directory $source
        Assert-Defaults $path
        Assert-Equal 8 ([IO.File]::ReadAllLines($path).Count) 'Duplicate defaults remained in the file.'
    }
    'quoted directive names cannot leave hidden overrides' = {
        param($directory)
        $path = New-TestConfig $directory "`"port`" 6380`n'port' 6381`nport 6382`n"
        Assert-Defaults $path
        Assert-Equal 8 ([IO.File]::ReadAllLines($path).Count) 'A quoted duplicate was not removed.'
    }
    'missing defaults are added once after unrelated content' = {
        param($directory)
        $path = New-TestConfig $directory "save 60 1`n# port 6380`nrequirepass 'example # password'"
        Assert-Defaults $path
        $lines = [IO.File]::ReadAllLines($path)
        Assert-Equal 'save 60 1' $lines[0] 'Unrelated setting changed.'
        Assert-Equal '# port 6380' $lines[1] 'Commented example changed.'
        Assert-Equal "requirepass 'example # password'" $lines[2] 'Password setting changed.'
        Assert-Equal 11 $lines.Count 'Unexpected lines were added.'
    }
    'empty source and repeated generation are deterministic' = {
        param($directory)
        $path = New-TestConfig $directory ''
        Assert-Defaults $path
        $second = Join-Path $directory 'second.conf'
        Write-ManagedRedisConfig -Source $path -Destination $second
        Assert-Equal ([IO.File]::ReadAllText($path)) ([IO.File]::ReadAllText($second)) 'Repeated generation added overrides.'
    }
    'UTF8 comments and CRLF input are preserved without a BOM' = {
        param($directory)
        $comment = '# ' + (-join ([char[]]@(0x6D4B, 0x8BD5)))
        $source = Join-Path $directory 'source.conf'
        $path = Join-Path $directory 'redis.conf'
        [IO.File]::WriteAllText($source, "$comment`r`nport 6379`r`n", (New-Object Text.UTF8Encoding($true)))
        Write-ManagedRedisConfig -Source $source -Destination $path
        Assert-Defaults $path
        Assert-Equal $comment ([IO.File]::ReadAllLines($path)[0]) 'UTF8 comment was changed.'
        $bytes = [IO.File]::ReadAllBytes($path)
        Assert-Equal $false ($bytes[0] -eq 0xef -and $bytes[1] -eq 0xbb -and $bytes[2] -eq 0xbf) 'Output contains a BOM.'
    }
    'existing destination is never overwritten' = {
        param($directory)
        $source = Join-Path $directory 'source.conf'
        $destination = Join-Path $directory 'redis.conf'
        [IO.File]::WriteAllText($source, 'port 6379', $utf8)
        [IO.File]::WriteAllText($destination, 'port 16379', $utf8)
        $failed = $false
        try { Write-ManagedRedisConfig -Source $source -Destination $destination } catch { $failed = $true }
        Assert-Equal $true $failed 'Existing configuration was accepted as a new destination.'
        Assert-Equal 'port 16379' ([IO.File]::ReadAllText($destination)) 'Existing configuration was overwritten.'
    }
    'invalid UTF8 is rejected instead of silently rewriting it' = {
        param($directory)
        $source = Join-Path $directory 'source.conf'
        $destination = Join-Path $directory 'redis.conf'
        [IO.File]::WriteAllBytes($source, [byte[]]@(0x23, 0x20, 0xff))
        $failed = $false
        try { Write-ManagedRedisConfig -Source $source -Destination $destination } catch { $failed = $true }
        Assert-Equal $true $failed 'Invalid UTF8 was silently replaced.'
        Assert-Equal $false ([IO.File]::Exists($destination)) 'Invalid source created a partial destination.'
    }
    'missing source does not create a destination' = {
        param($directory)
        $destination = Join-Path $directory 'redis.conf'
        $failed = $false
        try { Write-ManagedRedisConfig -Source (Join-Path $directory 'missing.conf') -Destination $destination } catch { $failed = $true }
        Assert-Equal $true $failed 'Missing source was accepted.'
        Assert-Equal $false ([IO.File]::Exists($destination)) 'Missing source created a partial destination.'
    }
    'directive matching is culture independent' = {
        param($directory)
        $previous = [Globalization.CultureInfo]::CurrentCulture
        try {
            [Globalization.CultureInfo]::CurrentCulture = [Globalization.CultureInfo]::GetCultureInfo('tr-TR')
            $path = New-TestConfig $directory (($defaults | ForEach-Object { $_.ToUpperInvariant() }) -join "`n")
            Assert-Defaults $path
            Assert-Equal 8 ([IO.File]::ReadAllLines($path).Count) 'Culture-dependent matching left duplicate records.'
        } finally { [Globalization.CultureInfo]::CurrentCulture = $previous }
    }
}

$failures = 0
foreach ($case in $cases.GetEnumerator()) {
    $directory = Join-Path ([IO.Path]::GetTempPath()) ('redis-config-test [space] ' + (-join ([char[]]@(0x6D4B, 0x8BD5))) + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $directory -ErrorAction Stop | Out-Null
    try {
        & $case.Value $directory
        Write-Host "PASS $($case.Key)"
    } catch {
        $failures++
        Write-Host "FAIL $($case.Key): $($_.Exception.Message)"
    } finally {
        # Only this test's freshly created, link-free directory is removed.
        if (-not [IO.Directory]::Exists($directory) -or
            (Get-Item -LiteralPath $directory -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Unsafe test cleanup directory.'
        }
        $links = @(Get-ChildItem -LiteralPath $directory -Force -Recurse | Where-Object {
            $_.Attributes -band [IO.FileAttributes]::ReparsePoint
        })
        if ($links.Count -ne 0) { throw 'Unexpected link in test cleanup directory.' }
        Remove-Item -LiteralPath $directory -Recurse -ErrorAction Stop
    }
}
Write-Host "Managed Redis config tests: $($cases.Count - $failures) passed, $failures failed."
if ($failures -ne 0) { exit 1 }
