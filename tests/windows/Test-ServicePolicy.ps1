param()
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# Load only the real policy functions; never create a service on the test host.
$source = Join-Path $PSScriptRoot '../../packaging/windows/scripts/Common-Redis.ps1'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$names = @('New-RedisService', 'Get-RedisServiceAccount', 'Set-RedisServiceAccount', 'Set-RedisServiceRecovery')
foreach ($name in $names) {
    $functions = @($ast.FindAll({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $name
    }, $false))
    if ($functions.Count -ne 1) { throw "Missing policy function: $name" }
    . ([scriptblock]::Create($functions[0].Extent.Text))
}
$script:RedisPrefix = $PSScriptRoot
$script:RedisServiceName = 'RedisPolicyFixture'
$script:Commands = @()
$script:Account = 'LocalSystem'
$script:ChangeStatus = 0
$script:ScStatus = 0
$script:CreatedCredential = $null
function Get-RedisService { return $null }
function New-Service {
    param($Name, $BinaryPathName, $DisplayName, $Description, $StartupType, $Credential)
    $script:CreatedCredential = $Credential
}
function sc.exe {
    $script:Commands += ($args -join '|')
    $global:LASTEXITCODE = $script:ScStatus
}
function Get-CimInstance {
    param($ClassName, $Filter)
    return [pscustomobject]@{ StartName = $script:Account }
}
function Invoke-CimMethod {
    param($InputObject, $MethodName, $Arguments)
    if ($script:ChangeStatus -eq 0) { $script:Account = $Arguments.StartName }
    return [pscustomobject]@{ ReturnValue = $script:ChangeStatus }
}
function Assert-Throws {
    param([scriptblock]$Action)
    $failed = $false
    try { & $Action } catch { $failed = $true }
    if (-not $failed) { throw 'Expected policy failure.' }
}

New-RedisService
if ($script:CreatedCredential.UserName -cne 'NT AUTHORITY\LocalService') { throw 'Service is not least-privileged.' }
if ($script:CreatedCredential.Password.Length -ne 0) { throw 'Built-in service account must not have a password.' }
if (@($script:Commands | Where-Object { $_ -like 'failure|*' }).Count -ne 1) { throw 'Service creation lost recovery policy.' }
if (@($script:Commands | Where-Object { $_ -like 'config|*' }).Count -ne 1) { throw 'Service creation lost delayed start.' }

Set-RedisServiceAccount -Account 'NT AUTHORITY\LocalService'
if ((Get-RedisServiceAccount) -cne 'NT AUTHORITY\LocalService') { throw 'Legacy account migration failed.' }
Set-RedisServiceAccount -Account 'LocalSystem'
if ((Get-RedisServiceAccount) -cne 'LocalSystem') { throw 'Legacy account rollback failed.' }
$script:Account = 'DOMAIN\custom'
Assert-Throws { Get-RedisServiceAccount }
$script:Account = 'LocalSystem'
$script:ChangeStatus = 5
Assert-Throws { Set-RedisServiceAccount -Account 'NT AUTHORITY\LocalService' }
if ($script:Account -cne 'LocalSystem') { throw 'Failed migration changed account.' }
$script:ScStatus = 5
Assert-Throws { Set-RedisServiceRecovery }
$global:LASTEXITCODE = 0
Write-Host 'Passed Windows service policy mock tests (creation, migration, rollback, rejection, recovery).'
