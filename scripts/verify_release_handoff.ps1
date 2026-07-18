param(
    [string]$ArtifactRoot,
    [string]$ExpectedTag,
    [string]$ExpectedSha,
    [string]$ExpectedVersion = "0.1.15",
    [string]$ZipName = "att-mz-windows-x86_64.zip",
    [string]$ExpectedZipSha256,
    [string]$ExpectedPylockSha256,
    [string]$ExpectedManifestSha256,
    [string]$ExpectedSumsSha256,
    [switch]$SelfTest
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-HexSha256 {
    param([string]$Value, [string]$Label)
    if ($Value -cnotmatch '^[0-9a-f]{64}$') { throw "$Label 不是小写 64 位 SHA-256：$Value" }
}

function Assert-ZipBasename {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.Trim() -cne $Value -or
        [IO.Path]::IsPathRooted($Value) -or [IO.Path]::GetFileName($Value) -cne $Value -or
        $Value -in @(".", "..") -or $Value.Contains("..") -or $Value.Contains("/") -or
        $Value.Contains("\") -or !$Value.EndsWith(".zip", [StringComparison]::OrdinalIgnoreCase) -or
        $Value.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0) {
        throw "ZipName 必须是单个安全的 .zip basename：$Value"
    }
}

function Assert-ArtifactPayload {
    param(
        [string]$Root,
        [string]$Tag,
        [string]$CommitSha,
        [string]$Version,
        [string]$ArchiveName,
        [hashtable]$ExpectedHashes
    )
    $resolvedRoot = [IO.Path]::GetFullPath($Root)
    $rootItem = Get-Item -LiteralPath $resolvedRoot -Force -ErrorAction Stop
    if (!$rootItem.PSIsContainer -or ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "publish artifact 根必须是普通、非 reparse point 目录：$resolvedRoot"
    }
    $expectedFiles = @(
        $ArchiveName,
        "SHA256SUMS.txt",
        "pylock.windows-x86_64.toml",
        "release-manifest.json"
    ) | Sort-Object
    $entries = @(Get-ChildItem -LiteralPath $resolvedRoot -Force)
    if ($entries | Where-Object { $_.PSIsContainer -or ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) }) {
        throw "publish artifact 只能包含四个普通文件"
    }
    $actualFiles = @($entries | Select-Object -ExpandProperty Name | Sort-Object)
    if (Compare-Object $expectedFiles $actualFiles) {
        throw "publish artifact 文件集合异常：expected=$($expectedFiles -join ',') actual=$($actualFiles -join ',')"
    }

    $externalHashNames = @(
        $ArchiveName,
        "SHA256SUMS.txt",
        "pylock.windows-x86_64.toml",
        "release-manifest.json"
    )
    foreach ($name in $externalHashNames) {
        $expectedHash = [string]$ExpectedHashes[$name]
        Assert-HexSha256 $expectedHash "verify job 提供的 $name 哈希"
        $actualHash = Get-Sha256 (Join-Path $resolvedRoot $name)
        if ($actualHash -cne $expectedHash) {
            throw "跨 job artifact SHA-256 不一致：name=$name expected=$expectedHash actual=$actualHash"
        }
    }

    $sumsText = Get-Content -Raw -LiteralPath (Join-Path $resolvedRoot "SHA256SUMS.txt") -Encoding ascii
    $sumLines = @($sumsText.Replace("`r`n", "`n").TrimEnd([char[]]"`n") -split "`n")
    $expectedSumNames = @($ArchiveName, "pylock.windows-x86_64.toml", "release-manifest.json") | Sort-Object
    $sums = @{}
    foreach ($line in $sumLines) {
        if ($line -notmatch '^([0-9a-f]{64})  ([A-Za-z0-9._-]+)$') { throw "SHA256SUMS 行非法：$line" }
        if ($sums.ContainsKey($Matches[2])) { throw "SHA256SUMS 含重复文件：$($Matches[2])" }
        $sums[$Matches[2]] = $Matches[1]
    }
    if (Compare-Object $expectedSumNames @($sums.Keys | Sort-Object)) {
        throw "SHA256SUMS 文件集合异常：$($sums.Keys -join ',')"
    }
    foreach ($name in $expectedSumNames) {
        $actualHash = Get-Sha256 (Join-Path $resolvedRoot $name)
        if ($actualHash -cne $sums[$name]) { throw "SHA256SUMS 内容不一致：$name" }
    }

    try {
        $manifest = Get-Content -Raw -LiteralPath (Join-Path $resolvedRoot "release-manifest.json") |
            ConvertFrom-Json -Depth 100 -ErrorAction Stop
    }
    catch {
        throw "release-manifest.json 不是有效 JSON 对象"
    }
    $zipHash = Get-Sha256 (Join-Path $resolvedRoot $ArchiveName)
    $pylockHash = Get-Sha256 (Join-Path $resolvedRoot "pylock.windows-x86_64.toml")
    $manifestPylockProperty = $manifest.sha256.PSObject.Properties["pylock.windows-x86_64.toml"]
    if ($null -eq $manifestPylockProperty -or $manifest.schema_version -ne 1 -or
        $manifest.version -cne $Version -or $manifest.tag -cne $Tag -or
        $manifest.commit -cne $CommitSha -or $manifest.platform -cne "windows-x86_64" -or
        $manifest.artifact.name -cne $ArchiveName -or $manifest.artifact.sha256 -cne $zipHash -or
        $manifestPylockProperty.Value -cne $pylockHash) {
        throw "release manifest 与已验证身份或 artifact 不一致"
    }
}

function Assert-GitIdentity {
    param([string]$Tag, [string]$CommitSha)
    if ($Tag -cne "v0.1.15" -or $CommitSha -cnotmatch '^[0-9a-f]{40}$') {
        throw "verify job 输出的发布身份非法：tag=$Tag sha=$CommitSha"
    }
    $fullTagRef = "refs/tags/$Tag"
    $headSha = (git rev-parse HEAD).Trim().ToLowerInvariant()
    $localTagSha = (git rev-parse "$fullTagRef^{commit}").Trim().ToLowerInvariant()
    if ($headSha -cne $CommitSha -or $localTagSha -cne $CommitSha) {
        throw "publish checkout 身份漂移：expected=$CommitSha tag=$localTagSha HEAD=$headSha"
    }

    $remoteLines = @(git ls-remote --tags origin $fullTagRef "$fullTagRef^{}")
    if ($LASTEXITCODE -ne 0 -or $remoteLines.Count -eq 0) { throw "无法读取远端发布标签：$fullTagRef" }
    $remoteRefs = @{}
    foreach ($line in $remoteLines) {
        if ($line -notmatch '^([0-9a-fA-F]{40})\s+(.+)$') { throw "远端 tag 输出非法：$line" }
        if ($remoteRefs.ContainsKey($Matches[2])) { throw "远端 tag 输出包含重复 ref：$($Matches[2])" }
        $remoteRefs[$Matches[2]] = $Matches[1].ToLowerInvariant()
    }
    $unexpectedRefs = @($remoteRefs.Keys | Where-Object { $_ -cne $fullTagRef -and $_ -cne "$fullTagRef^{}" })
    if ($unexpectedRefs.Count -ne 0 -or !$remoteRefs.ContainsKey($fullTagRef)) {
        throw "远端 tag ref 集合异常：$($remoteRefs.Keys -join ', ')"
    }
    $remotePeeledSha = if ($remoteRefs.ContainsKey("$fullTagRef^{}")) {
        $remoteRefs["$fullTagRef^{}"]
    }
    else {
        $remoteRefs[$fullTagRef]
    }
    if ($remotePeeledSha -cne $CommitSha) {
        throw "远端 tag peeled commit 漂移：expected=$CommitSha remote=$remotePeeledSha"
    }
}

function Invoke-HandoffSelfTest {
    $root = Join-Path ([IO.Path]::GetTempPath()) "att-mz-handoff-selftest-$([guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Path $root | Out-Null
    try {
        $archiveName = "artifact.zip"
        Set-Content -LiteralPath (Join-Path $root $archiveName) -Value "zip" -Encoding utf8NoBOM
        Set-Content -LiteralPath (Join-Path $root "pylock.windows-x86_64.toml") -Value "lock" -Encoding utf8NoBOM
        $zipHash = Get-Sha256 (Join-Path $root $archiveName)
        $pylockHash = Get-Sha256 (Join-Path $root "pylock.windows-x86_64.toml")
        @{
            schema_version = 1
            version = "0.1.15"
            tag = "v0.1.15"
            commit = "a" * 40
            platform = "windows-x86_64"
            artifact = @{ name = $archiveName; sha256 = $zipHash }
            sha256 = @{ "pylock.windows-x86_64.toml" = $pylockHash }
        } | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $root "release-manifest.json") -Encoding utf8NoBOM
        $manifestHash = Get-Sha256 (Join-Path $root "release-manifest.json")
        @(
            "$zipHash  $archiveName",
            "$pylockHash  pylock.windows-x86_64.toml",
            "$manifestHash  release-manifest.json"
        ) | Sort-Object | Set-Content -LiteralPath (Join-Path $root "SHA256SUMS.txt") -Encoding ascii
        $hashes = @{
            $archiveName = $zipHash
            "pylock.windows-x86_64.toml" = $pylockHash
            "release-manifest.json" = $manifestHash
            "SHA256SUMS.txt" = Get-Sha256 (Join-Path $root "SHA256SUMS.txt")
        }
        Assert-ArtifactPayload $root "v0.1.15" ("a" * 40) "0.1.15" $archiveName $hashes

        Add-Content -LiteralPath (Join-Path $root $archiveName) -Value "tampered" -Encoding utf8NoBOM
        $tamperRejected = $false
        try { Assert-ArtifactPayload $root "v0.1.15" ("a" * 40) "0.1.15" $archiveName $hashes }
        catch { $tamperRejected = $true }
        if (!$tamperRejected) { throw "artifact 篡改拒绝自测失败" }

        Set-Content -LiteralPath (Join-Path $root $archiveName) -Value "zip" -Encoding utf8NoBOM
        $badManifest = Get-Content -Raw -LiteralPath (Join-Path $root "release-manifest.json") | ConvertFrom-Json
        $badManifest.tag = "v0.1.99"
        $badManifest | ConvertTo-Json -Depth 10 | Set-Content `
            -LiteralPath (Join-Path $root "release-manifest.json") `
            -Encoding utf8NoBOM
        $badManifestHash = Get-Sha256 (Join-Path $root "release-manifest.json")
        @(
            "$zipHash  $archiveName",
            "$pylockHash  pylock.windows-x86_64.toml",
            "$badManifestHash  release-manifest.json"
        ) | Sort-Object | Set-Content -LiteralPath (Join-Path $root "SHA256SUMS.txt") -Encoding ascii
        $hashes["release-manifest.json"] = $badManifestHash
        $hashes["SHA256SUMS.txt"] = Get-Sha256 (Join-Path $root "SHA256SUMS.txt")
        $manifestRejected = $false
        try { Assert-ArtifactPayload $root "v0.1.15" ("a" * 40) "0.1.15" $archiveName $hashes }
        catch { $manifestRejected = $true }
        if (!$manifestRejected) { throw "manifest 身份漂移拒绝自测失败" }
        @{
            status = "ok"
            valid_handoff = $true
            tamper_rejected = $true
            manifest_identity_rejected = $true
        } | ConvertTo-Json -Compress
    }
    finally {
        if (Test-Path -LiteralPath $root) {
            $item = Get-Item -LiteralPath $root -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "拒绝清理被替换为 reparse point 的 handoff 自测目录：$root"
            }
            Remove-Item -LiteralPath $root -Recurse -Force
        }
    }
}

if ($SelfTest) {
    Invoke-HandoffSelfTest
    return
}

Assert-ZipBasename $ZipName
$ExpectedSha = $ExpectedSha.Trim().ToLowerInvariant()
$hashes = @{
    $ZipName = $ExpectedZipSha256
    "pylock.windows-x86_64.toml" = $ExpectedPylockSha256
    "release-manifest.json" = $ExpectedManifestSha256
    "SHA256SUMS.txt" = $ExpectedSumsSha256
}
Assert-ArtifactPayload $ArtifactRoot $ExpectedTag $ExpectedSha $ExpectedVersion $ZipName $hashes
Assert-GitIdentity $ExpectedTag $ExpectedSha
@{ status = "ok"; tag = $ExpectedTag; commit = $ExpectedSha; artifact = $ZipName } | ConvertTo-Json -Compress
