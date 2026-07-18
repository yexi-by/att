param(
    [string]$ExpectedVersion = "0.1.15",
    [string]$ZipName = "att-mz-windows-x86_64.zip",
    [string]$DistA = "dist-a",
    [string]$DistB = "dist-b",
    [switch]$ValidateInputsOnly,
    [switch]$SelfTest
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))

function Test-SamePath {
    param([string]$Left, [string]$Right)
    return [string]::Equals(
        [IO.Path]::TrimEndingDirectorySeparator([IO.Path]::GetFullPath($Left)),
        [IO.Path]::TrimEndingDirectorySeparator([IO.Path]::GetFullPath($Right)),
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-NormalDirectory {
    param([string]$Path, [string]$Label)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (!$item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "$Label 必须是普通、非 reparse point 目录：$Path"
    }
}

function Resolve-OutputDirectory {
    param([string]$Value, [string]$Label)
    if ([string]::IsNullOrWhiteSpace($Value)) { throw "$Label 不得为空" }
    $rawParts = @($Value -split '[\\/]+' | Where-Object { $_ -ne "" })
    if ($rawParts -contains "..") { throw "$Label 不得包含 '..'：$Value" }
    $candidate = if ([IO.Path]::IsPathRooted($Value)) {
        [IO.Path]::GetFullPath($Value)
    }
    else {
        [IO.Path]::GetFullPath((Join-Path $projectRoot $Value))
    }
    if ((Test-SamePath $candidate $projectRoot) -or
        !(Test-SamePath ([IO.Path]::GetDirectoryName($candidate)) $projectRoot)) {
        throw "$Label 必须是工作区的直接子目录：$Value"
    }
    $candidateAttributes = $null
    try {
        $candidateAttributes = [IO.File]::GetAttributes($candidate)
    }
    catch [IO.FileNotFoundException] {
        $candidateAttributes = $null
    }
    catch [IO.DirectoryNotFoundException] {
        $candidateAttributes = $null
    }
    if ($null -ne $candidateAttributes) {
        if ($candidateAttributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "$Label 不得是 reparse point：$candidate"
        }
        Assert-NormalDirectory $candidate $Label
        $resolved = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $candidate).Path)
        if (!(Test-SamePath ([IO.Path]::GetDirectoryName($resolved)) $projectRoot)) {
            throw "$Label 解析后越出工作区直接子目录：$candidate"
        }
    }
    return $candidate
}

function Assert-ZipBasename {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.Trim() -cne $Value) {
        throw "ZipName 不得为空或包含首尾空白"
    }
    if ([IO.Path]::IsPathRooted($Value) -or [IO.Path]::GetFileName($Value) -cne $Value -or
        $Value -in @(".", "..") -or $Value.Contains("..") -or $Value.Contains("/") -or
        $Value.Contains("\") -or !$Value.EndsWith(".zip", [StringComparison]::OrdinalIgnoreCase) -or
        $Value.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0) {
        throw "ZipName 必须是单个安全的 .zip basename：$Value"
    }
}

function Assert-CleanWorkspace {
    param([string]$Stage)
    $status = @(git status --porcelain=v1 --untracked-files=all)
    if ($LASTEXITCODE -ne 0) { throw "$Stage 无法读取 git 工作区状态" }
    if ($status.Count -ne 0) {
        throw "$Stage 要求干净工作区，发现：$($status -join '; ')"
    }
}

Assert-NormalDirectory $projectRoot "发布工作区"

function Resolve-ReleaseInputs {
    param([string]$FirstDist, [string]$SecondDist, [string]$ArchiveName)
    Assert-ZipBasename $ArchiveName
    $firstPath = Resolve-OutputDirectory $FirstDist "DistA"
    $secondPath = Resolve-OutputDirectory $SecondDist "DistB"
    if (Test-SamePath $firstPath $secondPath) {
        throw "DistA 与 DistB 必须是两个不同目录"
    }
    return [pscustomobject]@{ DistA = $firstPath; DistB = $secondPath; ZipName = $ArchiveName }
}

function Assert-InputRejected {
    param([scriptblock]$Operation, [string]$Label)
    $rejected = $false
    try { & $Operation | Out-Null }
    catch { $rejected = $true }
    if (!$rejected) { throw "输入安全自测未拒绝：$Label" }
}

function Invoke-InputSafetySelfTest {
    $valid = Resolve-ReleaseInputs "release-selftest-a" "release-selftest-b" "artifact.zip"
    if (!(Test-SamePath ([IO.Path]::GetDirectoryName($valid.DistA)) $projectRoot)) {
        throw "有效直接子目录自测失败"
    }
    Assert-InputRejected { Resolve-ReleaseInputs "same" "same" "artifact.zip" } "DistA=DistB"
    Assert-InputRejected { Resolve-ReleaseInputs "outer\nested" "other" "artifact.zip" } "嵌套 DistA"
    Assert-InputRejected { Resolve-ReleaseInputs "..\outside" "other" "artifact.zip" } "DistA 含 .."
    Assert-InputRejected { Resolve-ReleaseInputs "first" "second" "C:\absolute.zip" } "绝对 ZipName"

    $junctionPath = Join-Path $projectRoot ".release-safety-junction-$([guid]::NewGuid().ToString('N'))"
    try {
        New-Item -ItemType Junction -Path $junctionPath -Target ([IO.Path]::GetTempPath()) | Out-Null
        Assert-InputRejected { Resolve-ReleaseInputs $junctionPath "other" "artifact.zip" } "reparse point DistA"
    }
    finally {
        if (Test-Path -LiteralPath $junctionPath) {
            $junction = Get-Item -LiteralPath $junctionPath -Force
            if (!($junction.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw "拒绝清理不是 reparse point 的自测 junction：$junctionPath"
            }
            Remove-Item -LiteralPath $junctionPath -Force
        }
    }
    @{ status = "ok"; rejected = @("same", "nested", "parent", "absolute_zip", "reparse") } |
        ConvertTo-Json -Compress
}

if ($SelfTest) {
    Invoke-InputSafetySelfTest
    return
}

$resolvedInputs = Resolve-ReleaseInputs $DistA $DistB $ZipName
$distAPath = $resolvedInputs.DistA
$distBPath = $resolvedInputs.DistB
if ($ValidateInputsOnly) {
    @{
        status = "ok"
        dist_a = $distAPath
        dist_b = $distBPath
        zip_name = $resolvedInputs.ZipName
    } | ConvertTo-Json -Compress
    return
}

Push-Location $projectRoot
try {
    Assert-CleanWorkspace "发布验证开始前"
    $toolchain = Get-Content -Raw -LiteralPath "release-toolchain.lock.json" | ConvertFrom-Json
    foreach ($name in @("uv.lock", "rust/Cargo.lock")) {
        $expected = $toolchain.locks.PSObject.Properties[$name].Value
        $actual = (Get-FileHash -LiteralPath $name -Algorithm SHA256).Hash.ToLowerInvariant()
        if (!$expected -or $actual -cne $expected) {
            throw "依赖锁文件漂移：$name expected=$expected actual=$actual"
        }
    }

    uv python install 3.14.6
    uv sync --locked --dev --python 3.14.6
    uv lock --check

    $pythonVersion = (uv version --short).Trim()
    $cargo = cargo metadata --manifest-path rust/Cargo.toml --locked --no-deps --format-version 1 | ConvertFrom-Json
    $rustVersion = ($cargo.packages | Where-Object name -eq "att-mz-native").version
    if ($pythonVersion -cne $ExpectedVersion -or $rustVersion -cne $ExpectedVersion) {
        throw "版本不一致：expected=$ExpectedVersion python=$pythonVersion rust=$rustVersion"
    }
    if ((uv --version) -notmatch "^uv 0\.11\.28\b") { throw "uv 版本漂移" }
    if ((rustc --version) -notmatch "^rustc 1\.97\.0\b") { throw "Rust 版本漂移" }
    if ((uv run --locked maturin --version) -notmatch "1\.13\.1$") { throw "maturin 版本漂移" }

    uv run --locked python -m scripts.release_safety_selftest
    $pwshPath = (Get-Process -Id $PID).Path
    & $pwshPath -NoProfile -File scripts/run_release_verification.ps1 -SelfTest
    & $pwshPath -NoProfile -File scripts/smoke_release_windows.ps1 -SelfTest
    & $pwshPath -NoProfile -File scripts/verify_release_handoff.ps1 -SelfTest

    uv run --locked ruff format --check .
    uv run --locked ruff check .
    uv run --locked basedpyright

    $env:ATT_MZ_RUST_THREADS = "1"
    uv run --locked pytest -q -n 12 --dist=load --durations=30 --durations-min=0.5

    cargo fmt --manifest-path rust/Cargo.toml --all -- --check
    cargo clippy --manifest-path rust/Cargo.toml --locked --all-targets --all-features -- -D warnings
    cargo test --manifest-path rust/Cargo.toml --locked
    cargo build --manifest-path rust/Cargo.toml --locked --release --bin att-mz

    uv run --locked maturin develop --release --locked
    uv run --locked pytest -q `
        tests/test_native_adapters.py `
        tests/test_agent_toolkit.py `
        tests/test_event_command_text.py `
        tests/test_plugin_source_text.py `
        tests/test_plugin_text.py `
        tests/test_rmmz_loader_extraction_writeback.py `
        tests/test_terminology.py `
        tests/test_self_check.py
    uv run --locked python scripts/generate_skill_protocol.py --check

    Assert-CleanWorkspace "发行包构建前"

    uv run --locked python scripts/build_release.py --output-dir $distAPath --zip-name $ZipName
    uv run --locked python scripts/build_release.py --output-dir $distBPath --zip-name $ZipName

    $relativePaths = @(
        $ZipName,
        "pylock.windows-x86_64.toml",
        "release-manifest.json",
        "SHA256SUMS.txt",
        "_build\wheels\att_mz-$ExpectedVersion-cp314-cp314-win_amd64.whl",
        "att-mz\att-mz.exe",
        "att-mz\runtime\python.exe"
    )
    foreach ($relativePath in $relativePaths) {
        $left = Join-Path $distAPath $relativePath
        $right = Join-Path $distBPath $relativePath
        if (!(Test-Path -LiteralPath $left -PathType Leaf) -or !(Test-Path -LiteralPath $right -PathType Leaf)) {
            throw "缺少可复现性比较文件：$relativePath"
        }
        $leftHash = (Get-FileHash -LiteralPath $left -Algorithm SHA256).Hash
        $rightHash = (Get-FileHash -LiteralPath $right -Algorithm SHA256).Hash
        if ($leftHash -cne $rightHash) {
            throw "两次构建结果不一致：$relativePath"
        }
    }

    $nativeRelativeRoot = "att-mz\runtime\Lib\site-packages\app"
    $leftNative = @(Get-ChildItem -LiteralPath (Join-Path $distAPath $nativeRelativeRoot) -Filter "_native*.pyd")
    $rightNative = @(Get-ChildItem -LiteralPath (Join-Path $distBPath $nativeRelativeRoot) -Filter "_native*.pyd")
    if ($leftNative.Count -ne 1 -or $rightNative.Count -ne 1) {
        throw "发行包必须且只能包含一个 app._native 扩展模块"
    }
    if ($leftNative[0].Name -cne $rightNative[0].Name) {
        throw "两次构建的原生模块文件名不一致"
    }
    if ((Get-FileHash -LiteralPath $leftNative[0].FullName -Algorithm SHA256).Hash -cne
        (Get-FileHash -LiteralPath $rightNative[0].FullName -Algorithm SHA256).Hash) {
        throw "两次构建的原生模块内容不一致"
    }

    $suffix = [guid]::NewGuid().ToString("N").Substring(0, 8)
    $userName = "attmz-$suffix"
    $passwordText = "AttMz!$([guid]::NewGuid().ToString('N'))aA1"
    $password = ConvertTo-SecureString $passwordText -AsPlainText -Force
    $credential = [pscredential]::new("$env:COMPUTERNAME\$userName", $password)
    $shared = [IO.Path]::GetFullPath("C:\att-mz-standard-user-smoke-$suffix")
    if (!$shared.StartsWith("C:\att-mz-standard-user-smoke-", [StringComparison]::OrdinalIgnoreCase)) {
        throw "普通用户冒烟目录越界：$shared"
    }
    $developerModePath = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock"
    $developerModeName = "AllowDevelopmentWithoutDevLicense"
    $developerModeKeyExisted = Test-Path -LiteralPath $developerModePath
    $developerModeValueExisted = $false
    $developerModePreviousValue = $null
    $userCreated = $false
    try {
        if ($developerModeKeyExisted) {
            try {
                $developerModePreviousValue = Get-ItemPropertyValue `
                    -LiteralPath $developerModePath `
                    -Name $developerModeName `
                    -ErrorAction Stop
                $developerModeValueExisted = $true
            }
            catch {
                $developerModeValueExisted = $false
            }
        }
        New-Item -Path $developerModePath -Force | Out-Null
        if ($developerModeValueExisted) {
            Set-ItemProperty -LiteralPath $developerModePath -Name $developerModeName -Value ([int]0)
        }
        else {
            New-ItemProperty `
                -LiteralPath $developerModePath `
                -Name $developerModeName `
                -PropertyType DWord `
                -Value 0 `
                -Force | Out-Null
        }
        if ((Get-ItemPropertyValue -LiteralPath $developerModePath -Name $developerModeName) -ne 0) {
            throw "无法关闭 Developer Mode，普通用户符号链接冒烟无效"
        }

        New-LocalUser -Name $userName -Password $password -PasswordNeverExpires | Out-Null
        $userCreated = $true
        New-Item -ItemType Directory -Force -Path $shared | Out-Null
        Copy-Item -LiteralPath (Join-Path $distAPath $ZipName) -Destination (Join-Path $shared "release.zip")
        Copy-Item -LiteralPath "scripts\smoke_release_windows.ps1" -Destination (Join-Path $shared "smoke.ps1")
        icacls $shared /grant "${userName}:(OI)(CI)F" /T | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "无法授予普通用户冒烟目录权限" }

        $process = Start-Process pwsh `
            -Credential $credential `
            -LoadUserProfile `
            -Wait `
            -PassThru `
            -WindowStyle Hidden `
            -WorkingDirectory $shared `
            -ArgumentList @(
                "-NoProfile",
                "-File",
                (Join-Path $shared "smoke.ps1"),
                "-ZipPath",
                (Join-Path $shared "release.zip"),
                "-ResultPath",
                (Join-Path $shared "result.json")
            )
        if ($process.ExitCode -ne 0) {
            Get-Content -LiteralPath (Join-Path $shared "failure.txt") -ErrorAction SilentlyContinue
            throw "普通用户冒烟失败，退出码 $($process.ExitCode)"
        }
        $result = Get-Content -Raw -LiteralPath (Join-Path $shared "result.json") | ConvertFrom-Json
        if ($result.status -cne "ok" -or $result.version -cne "att-mz $ExpectedVersion" -or
            $result.symlink_creation -cne "denied" -or $result.symlink_error_code -ne 1314 -or
            $result.commands.version.exit -ne 0 -or $result.commands.version.stderr_lines -ne 0 -or
            $result.commands.help.exit -ne 0 -or $result.commands.help.stderr_lines -ne 0 -or
            $result.commands.list.exit -ne 0 -or $result.commands.list.stderr_lines -ne 2 -or
            $result.commands.self_check.exit -ne 0 -or $result.commands.self_check.stderr_lines -ne 3) {
            throw "普通用户冒烟结果不符合契约：$($result | ConvertTo-Json -Compress)"
        }
    }
    finally {
        if ($userCreated) {
            Remove-LocalUser -Name $userName -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $shared) {
            $resolvedShared = [IO.Path]::GetFullPath($shared)
            if (!$resolvedShared.StartsWith("C:\att-mz-standard-user-smoke-", [StringComparison]::OrdinalIgnoreCase)) {
                throw "拒绝清理越界普通用户冒烟目录：$resolvedShared"
            }
            Remove-Item -LiteralPath $resolvedShared -Recurse -Force -ErrorAction SilentlyContinue
        }
        if ($developerModeValueExisted) {
            Set-ItemProperty -LiteralPath $developerModePath -Name $developerModeName -Value $developerModePreviousValue
            if ((Get-ItemPropertyValue -LiteralPath $developerModePath -Name $developerModeName) -ne
                $developerModePreviousValue) {
                throw "Developer Mode 原值恢复失败"
            }
        }
        elseif ($developerModeKeyExisted) {
            Remove-ItemProperty -LiteralPath $developerModePath -Name $developerModeName -ErrorAction Stop
            try {
                $unexpectedValue = Get-ItemPropertyValue `
                    -LiteralPath $developerModePath `
                    -Name $developerModeName `
                    -ErrorAction Stop
                throw "Developer Mode 原缺失值恢复失败：$unexpectedValue"
            }
            catch [System.Management.Automation.PSArgumentException] {
            }
        }
        else {
            Remove-Item -LiteralPath $developerModePath -Force -ErrorAction Stop
            if (Test-Path -LiteralPath $developerModePath) { throw "Developer Mode 原缺失键恢复失败" }
        }
    }
}
finally {
    Pop-Location
}
