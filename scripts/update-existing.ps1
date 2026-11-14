param(
    [Parameter(Mandatory=$true)][string]$Source,
    [string]$Destination = 'E:\TelegramBatchDownloader',
    [switch]$CheckOnly,
    [switch]$NoLaunch
)
$ErrorActionPreference = 'Stop'
$sourceRoot = (Resolve-Path -LiteralPath $Source).Path.TrimEnd('\')
$targetRoot = [IO.Path]::GetFullPath($Destination).TrimEnd('\')
if (Test-Path -LiteralPath $targetRoot) { $targetRoot = (Resolve-Path -LiteralPath $targetRoot).Path.TrimEnd('\') }
if ($sourceRoot -eq $targetRoot -or $targetRoot -eq [IO.Path]::GetPathRoot($targetRoot).TrimEnd('\')) { throw 'Choose a separate application folder, not a drive root.' }
$sourceExe = Join-Path $sourceRoot 'TelegramBatchDownloader.exe'
$targetExe = Join-Path $targetRoot 'TelegramBatchDownloader.exe'
if (-not (Test-Path -LiteralPath $sourceExe -PathType Leaf) -or -not (Test-Path -LiteralPath (Join-Path $sourceRoot '_internal') -PathType Container)) { throw 'The full new application folder is required.' }
foreach ($rootPath in @($sourceRoot,$targetRoot)) {
    if ((Test-Path -LiteralPath $rootPath) -and ((Get-Item -LiteralPath $rootPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Application folders cannot be reparse points.' }
}
$active = @(Get-Process -Name 'TelegramBatchDownloader' -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.Equals($targetExe,[StringComparison]::OrdinalIgnoreCase) })
$sourceHash = (Get-FileHash -LiteralPath $sourceExe -Algorithm SHA256).Hash
if ($CheckOnly) {
    [pscustomobject]@{Source=$sourceRoot;Destination=$targetRoot;SourceVersion=(Get-Item -LiteralPath $sourceExe).VersionInfo.FileVersion;Running=$active.Count;SourceSHA256=$sourceHash;PreservedFolders=@('state','downloads');WritesPerformed=$false} | ConvertTo-Json
    return
}
if ($active.Count) { throw 'Close the old TelegramBatchDownloader window normally, then run this updater again.' }
New-Item -ItemType Directory -Path $targetRoot -Force | Out-Null
foreach ($name in @('_internal','docs','licenses')) {
    $sourcePath = Join-Path $sourceRoot $name
    $targetPath = Join-Path $targetRoot $name
    if (Test-Path -LiteralPath $targetPath) {
        $links = @(Get-Item -LiteralPath $targetPath -Force; Get-ChildItem -LiteralPath $targetPath -Force -Recurse) | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }
        if ($links) { throw "Cannot update linked application files: $name" }
    }
    if (Test-Path -LiteralPath $sourcePath -PathType Container) { Copy-Item -LiteralPath $sourcePath -Destination $targetRoot -Recurse -Force }
}
foreach ($name in @('README.md','CHANGELOG.md','LICENSE','THIRD-PARTY-NOTICES.md')) {
    $sourcePath = Join-Path $sourceRoot $name
    if (Test-Path -LiteralPath $sourcePath -PathType Leaf) { Copy-Item -LiteralPath $sourcePath -Destination $targetRoot -Force }
}
$staging = Join-Path $targetRoot ('.TelegramBatchDownloader-' + [guid]::NewGuid().ToString('N') + '.new')
Copy-Item -LiteralPath $sourceExe -Destination $staging
if ((Get-FileHash -LiteralPath $staging -Algorithm SHA256).Hash -ne $sourceHash) { throw 'Copied executable checksum failed. Retry the updater.' }
if (Test-Path -LiteralPath $targetExe -PathType Leaf) { [IO.File]::Replace($staging,$targetExe,[NullString]::Value) }
else { [IO.File]::Move($staging,$targetExe) }
Write-Host 'Update complete. Existing state and download folders are preserved.'
if (-not $NoLaunch) { Start-Process -FilePath $targetExe -WorkingDirectory $targetRoot -WindowStyle Normal }
