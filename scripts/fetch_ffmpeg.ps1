<#
.SYNOPSIS
  Downloads the pinned third-party binaries that get bundled into SuperDownloader.exe and
  verifies their SHA-256 checksums. Nothing is used unless the checksum matches.

  - FFmpeg 9.0.2 "essentials" build by Gyan Doshi (ffmpeg.exe, ffprobe.exe) -> vendor\ffmpeg\
    Merges video+audio into MP4 and converts audio to MP3.
  - Deno 2.9.6 (deno.exe, from the official @deno/win32-x64 npm package)  -> vendor\deno\
    Runs yt-dlp's YouTube JavaScript challenge solver (yt-dlp-ejs).

  Pinned checksums were verified against the publishers:
  - FFmpeg: identical from github.com/GyanD/codexffmpeg and gyan.dev, and equal to gyan.dev's
    published ffmpeg-9.0.2-essentials_build.zip.sha256.
  - Deno: npm registry tarball (registry sha512 integrity:
    qRGnmVz/Ea6UWPht/S3xFRK17UZ/ls38DSVfgGB17fpASLxPkksqzWUI/BzL9YYoOknYzfc94mySGyInZu/JXw==).

  To move to a newer build, change the version, URL and hash together.

.PARAMETER Force
  Re-download even if the pinned versions are already present.
#>
[CmdletBinding()]
param([switch]$Force)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root      = Split-Path -Parent $PSScriptRoot
$Vendor    = Join-Path $Root 'vendor'
$Downloads = Join-Path $Vendor '_downloads'

$Pins = @(
    @{
        Name    = 'FFmpeg'
        Version = '9.0.2'
        Urls    = @(
            'https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip',
            'https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip'
        )
        File    = 'ffmpeg-9.0.2-essentials_build.zip'
        Sha256  = '60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba'
        Dest    = 'ffmpeg'
        Wanted  = @('ffmpeg.exe', 'ffprobe.exe', 'LICENSE')
    },
    @{
        Name    = 'Deno'
        Version = '2.9.6'
        Urls    = @('https://registry.npmjs.org/@deno/win32-x64/-/win32-x64-2.9.6.tgz')
        File    = 'deno-win32-x64-2.9.6.tgz'
        Sha256  = '559b622e642210e0f0af0e4581953a0a9679a21968a57b7dc8a1b1a91d09b67f'
        Dest    = 'deno'
        Wanted  = @('deno.exe')
    }
)

function Get-Sha256([string]$Path) {
    (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-Verified($Pin) {
    New-Item -ItemType Directory -Force -Path $Downloads | Out-Null
    $archive = Join-Path $Downloads $Pin.File
    if ((Test-Path $archive) -and (Get-Sha256 $archive) -eq $Pin.Sha256) {
        Write-Host "  using cached $($Pin.File)"
        return $archive
    }
    foreach ($url in $Pin.Urls) {
        Write-Host "  downloading $url"
        try {
            Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing -TimeoutSec 600
        } catch {
            Write-Warning "  download failed: $($_.Exception.Message)"
            continue
        }
        $actual = Get-Sha256 $archive
        if ($actual -eq $Pin.Sha256) {
            Write-Host "  checksum OK ($actual)"
            return $archive
        }
        Remove-Item -Force $archive
        throw "$($Pin.Name): checksum mismatch for $url`n  expected $($Pin.Sha256)`n  got      $actual"
    }
    throw "$($Pin.Name): could not download from any source."
}

function Expand-Pin($Pin, [string]$Archive) {
    $dest = Join-Path $Vendor $Pin.Dest
    $work = Join-Path $Downloads ("extract-" + $Pin.Dest)
    if (Test-Path $work) { Remove-Item -Recurse -Force $work }
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    if ($Archive.EndsWith('.zip')) {
        Expand-Archive -Path $Archive -DestinationPath $work -Force
    } else {
        # tar.exe ships with Windows 10 1803+ and handles .tgz
        & tar.exe -xzf $Archive -C $work
        if ($LASTEXITCODE -ne 0) { throw "$($Pin.Name): tar failed with exit code $LASTEXITCODE" }
    }
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    foreach ($name in $Pin.Wanted) {
        $found = Get-ChildItem -Path $work -Recurse -File -Filter $name | Select-Object -First 1
        if (-not $found) { throw "$($Pin.Name): $name not found in $($Pin.File)" }
        Copy-Item $found.FullName (Join-Path $dest $name)
    }
    Set-Content -Path (Join-Path $dest 'VERSION.txt') -Value "$($Pin.Name) $($Pin.Version)`nsource: $($Pin.Urls[0])`nsha256: $($Pin.Sha256)" -Encoding UTF8
    Remove-Item -Recurse -Force $work
}

foreach ($pin in $Pins) {
    $dest = Join-Path $Vendor $pin.Dest
    $marker = Join-Path $dest 'VERSION.txt'
    $have = (Test-Path $marker) -and ((Get-Content $marker -Raw) -match [regex]::Escape($pin.Sha256))
    $allFiles = $true
    foreach ($name in $pin.Wanted) { if (-not (Test-Path (Join-Path $dest $name))) { $allFiles = $false } }
    if ($have -and $allFiles -and -not $Force) {
        Write-Host "$($pin.Name) $($pin.Version): already present"
        continue
    }
    Write-Host "$($pin.Name) $($pin.Version):"
    $archive = Get-Verified $pin
    Expand-Pin $pin $archive
    Write-Host "  installed to vendor\$($pin.Dest)"
}

# Capture the output first: piping straight into Select-Object -First 1 can stop the tool early
# and make its exit code non-zero even though it works.
$out = & (Join-Path $Vendor 'ffmpeg\ffmpeg.exe') -hide_banner -version
if ($LASTEXITCODE -ne 0) { throw 'ffmpeg.exe does not run' }
$out | Select-Object -First 1
$out = & (Join-Path $Vendor 'deno\deno.exe') --version
if ($LASTEXITCODE -ne 0) { throw 'deno.exe does not run' }
$out | Select-Object -First 1
Write-Host 'Third-party binaries ready.'
