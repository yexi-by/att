param(
    [string]$ZipPath,
    [string]$ResultPath,
    [switch]$SelfTest
)

$ErrorActionPreference = "Stop"

function Invoke-CapturedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [string[]]$Arguments = @()
    )

    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = [Text.UTF8Encoding]::new($false)
    $startInfo.StandardErrorEncoding = [Text.UTF8Encoding]::new($false)
    foreach ($argument in $Arguments) {
        $startInfo.ArgumentList.Add($argument)
    }

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (!$process.Start()) { throw "无法启动命令：$FilePath" }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            Stdout = $stdout
            Stderr = $stderr
        }
    }
    finally {
        $process.Dispose()
    }
}

function Assert-NoPrivilegeError {
    param([string]$Label, [pscustomobject]$Result)
    $combined = "$($Result.Stdout)`n$($Result.Stderr)"
    if ($combined -match '(?i)WinError\W*1314|A required privilege is not held by the client') {
        throw "$Label 出现 WinError 1314/权限错误：stdout=$($Result.Stdout) stderr=$($Result.Stderr)"
    }
}

function Get-StderrLines {
    param([string]$Text)
    $normalized = $Text.Replace("`r`n", "`n").Replace("`r", "`n").TrimEnd([char[]]"`n")
    if ($normalized.Length -eq 0) { return @() }
    $lines = @($normalized.Split([char]"`n"))
    if ($lines | Where-Object { $_.Length -eq 0 }) {
        throw "stderr 含未声明的空行：$Text"
    }
    return $lines
}

function Assert-StderrContract {
    param(
        [string]$Label,
        [string]$Stderr,
        [string[]]$AllowedLinePatterns
    )
    if ($Stderr -match '(?im)^(ERROR|WARNING|CRITICAL|Traceback)\b') {
        throw "$Label stderr 含错误级别输出：$Stderr"
    }
    $lines = @(Get-StderrLines $Stderr)
    if ($lines.Count -ne $AllowedLinePatterns.Count) {
        throw "$Label stderr 行数不符合契约：expected=$($AllowedLinePatterns.Count) actual=$($lines.Count) stderr=$Stderr"
    }
    for ($index = 0; $index -lt $AllowedLinePatterns.Count; $index++) {
        if ($lines[$index] -cnotmatch $AllowedLinePatterns[$index]) {
            throw "$Label stderr 第 $($index + 1) 行不符合契约：$($lines[$index])"
        }
    }
}

function Assert-CommandResult {
    param(
        [string]$Label,
        [pscustomobject]$Result,
        [string[]]$AllowedStderrPatterns
    )
    Assert-NoPrivilegeError $Label $Result
    if ($Result.ExitCode -ne 0) {
        throw "$Label 退出码错误：exit=$($Result.ExitCode) stdout=$($Result.Stdout) stderr=$($Result.Stderr)"
    }
    Assert-StderrContract $Label $Result.Stderr $AllowedStderrPatterns
}

function Convert-CommandJson {
    param([string]$Label, [string]$Stdout)
    try {
        $payload = $Stdout | ConvertFrom-Json -Depth 100 -ErrorAction Stop
    }
    catch {
        throw "$Label stdout 不是单一 JSON 对象：$Stdout"
    }
    if ($null -eq $payload -or $payload -isnot [pscustomobject]) {
        throw "$Label stdout JSON 必须是对象：$Stdout"
    }
    return $payload
}

function Invoke-CaptureSelfTest {
    $pwshPath = (Get-Process -Id $PID).Path
    $captured = Invoke-CapturedCommand `
        -FilePath $pwshPath `
        -WorkingDirectory $PSScriptRoot `
        -Arguments @(
            "-NoProfile",
            "-Command",
            "[Console]::Out.Write('stdout-probe'); [Console]::Error.Write('stderr-probe'); exit 7"
        )
    if ($captured.ExitCode -ne 7 -or $captured.Stdout -cne "stdout-probe" -or
        $captured.Stderr -cne "stderr-probe") {
        throw "命令捕获自测失败：$($captured | ConvertTo-Json -Compress)"
    }
    $privilegeRejected = $false
    try {
        Assert-NoPrivilegeError "self-test" ([pscustomobject]@{ Stdout = "WinError 1314"; Stderr = "" })
    }
    catch {
        $privilegeRejected = $true
    }
    if (!$privilegeRejected) { throw "WinError 1314 拒绝自测失败" }
    $extraStderrRejected = $false
    try { Assert-StderrContract "self-test" "INFO allowed`nERROR unexpected`n" @('^INFO allowed$') }
    catch { $extraStderrRejected = $true }
    if (!$extraStderrRejected) { throw "额外/错误 stderr 拒绝自测失败" }
    $nonzeroRejected = $false
    try {
        Assert-CommandResult "self-test" ([pscustomobject]@{ ExitCode = 9; Stdout = ""; Stderr = "" }) @()
    }
    catch { $nonzeroRejected = $true }
    if (!$nonzeroRejected) { throw "非零退出码拒绝自测失败" }
    @{
        status = "ok"
        captured_exit = 7
        privilege_error_rejected = $true
        extra_stderr_rejected = $true
        nonzero_exit_rejected = $true
    } | ConvertTo-Json -Compress
}

if ($SelfTest) {
    Invoke-CaptureSelfTest
    return
}
if ([string]::IsNullOrWhiteSpace($ZipPath) -or [string]::IsNullOrWhiteSpace($ResultPath)) {
    throw "正常冒烟必须同时提供 -ZipPath 与 -ResultPath"
}

$ZipPath = [IO.Path]::GetFullPath($ZipPath)
$ResultPath = [IO.Path]::GetFullPath($ResultPath)
$zipItem = Get-Item -LiteralPath $ZipPath -Force -ErrorAction Stop
if ($zipItem.PSIsContainer -or ($zipItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "冒烟 ZIP 必须是普通、非 reparse point 文件：$ZipPath"
}
$resultDirectory = Split-Path -Parent $ResultPath
$root = Join-Path $resultDirectory "A T T-普通用户-$([guid]::NewGuid())"

try {
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "冒烟进程拥有管理员权限"
    }

    New-Item -ItemType Directory -Path $root | Out-Null
    $linkTarget = Join-Path $root "link-target.txt"
    $linkPath = Join-Path $root "link-probe"
    Set-Content -LiteralPath $linkTarget -Value "probe" -Encoding utf8NoBOM
    $linkCreated = $false
    $symlinkDenialVerified = $false
    try {
        New-Item -ItemType SymbolicLink -Path $linkPath -Target $linkTarget -ErrorAction Stop | Out-Null
        $linkCreated = $true
    }
    catch {
        $inner = $_.Exception.InnerException
        $messages = "$($_.Exception.Message) $($inner?.Message)"
        if ($_.FullyQualifiedErrorId -cne "NewItemSymbolicLinkElevationRequired,Microsoft.PowerShell.Commands.NewItemCommand" -or
            $_.CategoryInfo.Category -ne [Management.Automation.ErrorCategory]::PermissionDenied -or
            $inner -isnot [ComponentModel.Win32Exception] -or $inner.NativeErrorCode -ne 1314 -or
            $messages -notmatch '(?i)privilege|特权') {
            throw "符号链接探针出现非预期异常，不能视为 WinError 1314：$($_ | Out-String)"
        }
        $symlinkDenialVerified = $true
    }
    if ($linkCreated) {
        Remove-Item -LiteralPath $linkPath -Force
        throw "当前普通用户仍可创建符号链接，无法证明 WinError 1314 场景已覆盖"
    }
    if (!$symlinkDenialVerified) { throw "符号链接探针未证明 WinError 1314 权限拒绝" }

    $extract = Join-Path $root "含 中文和空格\深层目录\发行 包"
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $extract
    $bundle = Join-Path $extract "att-mz"
    $appHome = Join-Path $root "isolated-home"
    New-Item -ItemType Directory -Path $appHome | Out-Null
    Copy-Item -LiteralPath (Join-Path $bundle "setting.toml") -Destination $appHome
    Copy-Item -LiteralPath (Join-Path $bundle "setting.example.toml") -Destination $appHome
    Copy-Item -LiteralPath (Join-Path $bundle "prompts") -Destination $appHome -Recurse
    Copy-Item -LiteralPath (Join-Path $bundle "fonts") -Destination $appHome -Recurse

    $env:ATT_MZ_HOME = $appHome
    $pexProbe = Join-Path $root "pex-root-probe"
    $scieProbe = Join-Path $root "scie-base-probe"
    $env:PEX_ROOT = $pexProbe
    $env:SCIE_BASE = $scieProbe
    Remove-Item Env:PEX_VERBOSE, Env:PYTHONHOME, Env:PYTHONPATH -ErrorAction SilentlyContinue
    $exe = Join-Path $bundle "att-mz.exe"

    $noStderr = @()
    $version = Invoke-CapturedCommand -FilePath $exe -WorkingDirectory $bundle -Arguments @("--version")
    Assert-CommandResult "--version" $version $noStderr
    $versionText = $version.Stdout.Trim()
    if ($versionText -cne "att-mz 0.1.15") { throw "--version stdout 错误：$($version.Stdout)" }

    $help = Invoke-CapturedCommand -FilePath $exe -WorkingDirectory $bundle -Arguments @("--help")
    Assert-CommandResult "--help" $help $noStderr
    if ($help.Stdout -cnotmatch '^usage: att-mz ' -or $help.Stdout -cnotmatch '(?m)^\s+list\s+' -or
        $help.Stdout -cnotmatch '(?m)^\s+self-check\s+') {
        throw "--help stdout 缺少稳定命令入口：$($help.Stdout)"
    }

    $startListPattern = '^INFO CLI 运行开始 \| 命令参数: list \| 解析参数: command=list, debug=False \| 工作目录: .+ \| 日志文件: .+[\\/]logs[\\/]app\.log$'
    $successEndPattern = '^INFO CLI 运行结束 状态 成功 退出码 0 耗时 \d+(?:\.\d+)? 秒$'
    $list = Invoke-CapturedCommand -FilePath $exe -WorkingDirectory $bundle -Arguments @("list")
    Assert-CommandResult "list" $list @($startListPattern, $successEndPattern)
    $listPayload = Convert-CommandJson "list" $list.Stdout
    if ($listPayload.status -notin @("ok", "warning") -or @($listPayload.errors).Count -ne 0) {
        throw "list JSON 契约失败：$($listPayload | ConvertTo-Json -Compress -Depth 100)"
    }

    $startCheckPattern = '^INFO CLI 运行开始 \| 命令参数: self-check --offline \| 解析参数: command=self-check, debug=False, offline=True \| 工作目录: .+ \| 日志文件: .+[\\/]logs[\\/]app\.log$'
    $configurationPattern = '^INFO 当前正在使用的配置 \| 配置文件: .+ \| 正文接口: .+$'
    $check = Invoke-CapturedCommand -FilePath $exe -WorkingDirectory $bundle -Arguments @("self-check", "--offline")
    Assert-CommandResult "self-check --offline" $check @(
        $startCheckPattern,
        $configurationPattern,
        $successEndPattern
    )
    $checkPayload = Convert-CommandJson "self-check --offline" $check.Stdout
    if ($checkPayload.status -cne "ok" -or @($checkPayload.errors).Count -ne 0 -or
        $checkPayload.summary.version -cne "0.1.15" -or $checkPayload.summary.offline -ne $true -or
        $checkPayload.summary.schema_version -ne 12 -or
        $checkPayload.details.checks.network_accessed -ne $false) {
        throw "self-check JSON 契约失败：$($checkPayload | ConvertTo-Json -Compress -Depth 100)"
    }

    $pexArtifacts = Get-ChildItem -LiteralPath $appHome -Recurse -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match "^(pex|scie)" }
    if ($pexArtifacts -or (Test-Path -LiteralPath $pexProbe) -or (Test-Path -LiteralPath $scieProbe)) {
        throw "运行后出现 PEX/scie 缓存"
    }

    @{
        status = "ok"
        user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        symlink_creation = "denied"
        version = $versionText
        symlink_error_code = 1314
        commands = @{
            version = @{ exit = $version.ExitCode; stderr_lines = 0 }
            help = @{ exit = $help.ExitCode; stderr_lines = 0 }
            list = @{ exit = $list.ExitCode; stderr_lines = 2 }
            self_check = @{ exit = $check.ExitCode; stderr_lines = 3 }
        }
    } | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ResultPath -Encoding utf8NoBOM
}
catch {
    $_ | Out-String | Set-Content -LiteralPath (Join-Path (Split-Path $ResultPath) "failure.txt") -Encoding utf8NoBOM
    exit 1
}
finally {
    if (Test-Path -LiteralPath $root) {
        $rootItem = Get-Item -LiteralPath $root -Force
        if ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "拒绝清理被替换为 reparse point 的冒烟目录：$root"
        }
        Remove-Item -LiteralPath $root -Recurse -Force
    }
}
