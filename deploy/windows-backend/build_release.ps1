param(
    [string]$Version = "v2.3.2",
    [string]$PythonVersion = "3.12.10"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($Version -notmatch '^v\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.]+)?$') {
    throw "Version must look like v2.1.0."
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$outputRoot = Join-Path $repoRoot "dist"
$cacheRoot = Join-Path $outputRoot "_downloads"
$packageName = "Moviemuse-Windows-Worker-$Version-win64"
$packageRoot = Join-Path $outputRoot $packageName
$archivePath = "$packageRoot.zip"
$pythonArchive = Join-Path $cacheRoot "python-$PythonVersion-embed-amd64.zip"
$pipZipApp = Join-Path $cacheRoot "pip.pyz"
$requirementsFile = Join-Path $repoRoot "requirements-windows-worker.lock.txt"
$gpuRequirementsFile = Join-Path $repoRoot "requirements-windows-worker-gpu.lock.txt"
$desktopRequirementsFile = Join-Path $repoRoot "requirements-windows-desktop-build.lock.txt"
$desktopLauncherSource = Join-Path $repoRoot "deploy\windows-backend\desktop_launcher.py"
$desktopBuildRoot = Join-Path $outputRoot "_desktop-build"
$desktopBuildVenv = Join-Path $desktopBuildRoot "venv"
$desktopBuildPython = Join-Path $desktopBuildVenv "Scripts\python.exe"
$pythonVersionParts = $PythonVersion.Split('.')
if ($pythonVersionParts.Count -lt 2) {
    throw "PythonVersion must look like 3.12.10."
}
$pythonCompactVersion = "$($pythonVersionParts[0])$($pythonVersionParts[1])"

function Assert-ReleaseTarget([string]$Path) {
    $fullPath = [IO.Path]::GetFullPath($Path)
    $allowedRoot = [IO.Path]::GetFullPath($outputRoot).TrimEnd('\') + '\'
    if (-not $fullPath.StartsWith($allowedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the release output directory: $fullPath"
    }
}

function Copy-ReleaseFile([string]$RelativePath) {
    $source = Join-Path $repoRoot ($RelativePath -replace '/', '\')
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Required release file is missing: $RelativePath"
    }
    $destination = Join-Path $packageRoot ($RelativePath -replace '/', '\')
    $destinationDir = Split-Path -Parent $destination
    New-Item -ItemType Directory -Path $destinationDir -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

New-Item -ItemType Directory -Path $cacheRoot -Force | Out-Null
Assert-ReleaseTarget $packageRoot
Assert-ReleaseTarget $archivePath

if (Test-Path -LiteralPath $packageRoot) {
    Remove-Item -LiteralPath $packageRoot -Recurse -Force
}
if (Test-Path -LiteralPath $archivePath) {
    Remove-Item -LiteralPath $archivePath -Force
}
New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null

if (-not (Test-Path -LiteralPath $pythonArchive -PathType Leaf)) {
    $pythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
    Write-Host "Downloading portable Python $PythonVersion..."
    Invoke-WebRequest -Uri $pythonUrl -OutFile $pythonArchive
}
if (-not (Test-Path -LiteralPath $pipZipApp -PathType Leaf)) {
    Write-Host "Downloading pip zipapp..."
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/pip/pip.pyz" -OutFile $pipZipApp
}

$pythonRoot = Join-Path $packageRoot "python"
Expand-Archive -LiteralPath $pythonArchive -DestinationPath $pythonRoot
$sitePackages = Join-Path $pythonRoot "Lib\site-packages"
New-Item -ItemType Directory -Path $sitePackages -Force | Out-Null
@(
    "python$pythonCompactVersion.zip"
    "."
    ".."
    "Lib\site-packages"
    "import site"
) | Set-Content -LiteralPath (Join-Path $pythonRoot "python$pythonCompactVersion._pth") -Encoding ASCII
Copy-Item -LiteralPath $pipZipApp -Destination (Join-Path $packageRoot "pip.pyz") -Force

Write-Host "Installing locked Windows worker dependencies..."
& (Join-Path $pythonRoot "python.exe") (Join-Path $packageRoot "pip.pyz") install `
    --disable-pip-version-check `
    --no-cache-dir `
    --find-links $cacheRoot `
    --target $sitePackages `
    -r $requirementsFile
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed with exit code $LASTEXITCODE."
}

$rootFiles = @(
    "allow_windows_backend_firewall.bat"
    "install_windows_worker.bat"
    "requirements-windows-worker.lock.txt"
    "requirements-windows-worker-gpu.lock.txt"
    "requirements.txt"
    "run_worker.py"
    "start_local_backend.bat"
    "start_windows_backend.bat"
)
foreach ($relativePath in $rootFiles) {
    Copy-ReleaseFile $relativePath
}

$appFiles = @(git -C $repoRoot ls-files -- app)
if ($LASTEXITCODE -ne 0 -or $appFiles.Count -eq 0) {
    throw "Could not enumerate tracked app files."
}
foreach ($relativePath in $appFiles) {
    Copy-ReleaseFile $relativePath
}
if ($appFiles -notcontains "app/worker_service.py") {
    Copy-ReleaseFile "app/worker_service.py"
}

$workerFrontendRoot = Join-Path $repoRoot "frontend"
Write-Host "Building lightweight Worker UI..."
Push-Location $workerFrontendRoot
try {
    & npm.cmd run build:worker
    if ($LASTEXITCODE -ne 0) {
        throw "Worker UI build failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}
$workerUiSource = Join-Path $workerFrontendRoot "worker-dist"
if (-not (Test-Path -LiteralPath (Join-Path $workerUiSource "index.html") -PathType Leaf)) {
    throw "Worker UI build did not produce index.html."
}
Copy-Item -LiteralPath $workerUiSource -Destination (Join-Path $packageRoot "worker-ui") -Recurse -Force

Write-Host "Building native MovieMuse Worker desktop window..."
if (-not (Test-Path -LiteralPath $desktopBuildPython -PathType Leaf)) {
    New-Item -ItemType Directory -Path $desktopBuildRoot -Force | Out-Null
    & py.exe -3.13 -m venv $desktopBuildVenv
    if ($LASTEXITCODE -ne 0) {
        throw "Desktop launcher build environment creation failed with exit code $LASTEXITCODE."
    }
}
& $desktopBuildPython -m pip install `
    --disable-pip-version-check `
    --quiet `
    -r $desktopRequirementsFile
if ($LASTEXITCODE -ne 0) {
    throw "Desktop launcher build dependency installation failed with exit code $LASTEXITCODE."
}

$numericVersion = $Version.TrimStart('v').Split('-', 2)[0].Split('.', 3)
$versionTuple = "$([int]$numericVersion[0]), $([int]$numericVersion[1]), $([int]$numericVersion[2]), 0"
$desktopVersionFile = Join-Path $desktopBuildRoot "MovieMuseWorker-version.txt"
@"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($versionTuple),
    prodvers=($versionTuple),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404B0',
        [StringStruct('CompanyName', 'MovieMuse'),
         StringStruct('FileDescription', 'MovieMuse Windows Worker'),
         StringStruct('FileVersion', '$($Version.TrimStart('v'))'),
         StringStruct('InternalName', 'MovieMuseWorker'),
         StringStruct('LegalCopyright', 'MovieMuse'),
         StringStruct('OriginalFilename', 'MovieMuseWorker.exe'),
         StringStruct('ProductName', 'MovieMuse Worker'),
         StringStruct('ProductVersion', '$($Version.TrimStart('v'))')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"@ | Set-Content -LiteralPath $desktopVersionFile -Encoding UTF8

$desktopWorkRoot = Join-Path $desktopBuildRoot "work"
$desktopSpecRoot = Join-Path $desktopBuildRoot "spec"
New-Item -ItemType Directory -Path $desktopWorkRoot -Force | Out-Null
New-Item -ItemType Directory -Path $desktopSpecRoot -Force | Out-Null
& $desktopBuildPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --noupx `
    --name MovieMuseWorker `
    --icon (Join-Path $repoRoot "app\static\icons\favicon.ico") `
    --version-file $desktopVersionFile `
    --hidden-import webview.platforms.edgechromium `
    --distpath $packageRoot `
    --workpath $desktopWorkRoot `
    --specpath $desktopSpecRoot `
    $desktopLauncherSource
if ($LASTEXITCODE -ne 0) {
    throw "Desktop launcher build failed with exit code $LASTEXITCODE."
}
if (-not (Test-Path -LiteralPath (Join-Path $packageRoot "MovieMuseWorker.exe") -PathType Leaf)) {
    throw "Desktop launcher build did not produce MovieMuseWorker.exe."
}

@"
MovieMuse Windows Worker $Version
================================

1. Double-click:
   MovieMuseWorker.exe

2. The Worker UI opens in its own desktop window. No command window or external browser is required.

3. Open the model page. If no compatible CUDA 12 / cuDNN 9 runtime is detected,
   use "Install GPU runtime" once. The download is about 1.2 GB and is stored in
   %LOCALAPPDATA%\MovieMuse Worker\gpu-runtime. Restart MovieMuseWorker.exe after installation.

4. Download or verify Whisper model files in the Worker UI. Models are stored in
   %LOCALAPPDATA%\MovieMuse Worker\whisper-models and survive Worker upgrades.

5. The LAN URL uses port 18181, for example:
   http://192.168.x.x:18181

6. In MovieMuse, choose "Auto scan", select this Worker, and enter the six-digit
   pairing code shown on the Worker overview page. Configure path mapping only if needed.

Compute settings and model activation remain controlled by MovieMuse.

Optional local path lock:
  set COMPUTE_ALLOWED_MEDIA_DIRS=\\NAS\media

Defaults:
  WHISPER_MODEL=large-v3-turbo
  WHISPER_DEVICE=cuda
  WHISPER_COMPUTE_TYPE=float16
  PORT=18181

This lightweight core package includes portable Python and Worker dependencies.
Large NVIDIA cuBLAS/cuDNN libraries and Whisper models are not included. If compatible
GPU runtime libraries already exist on Windows, the Worker reuses them instead of downloading copies.

If Microsoft Edge WebView2 Runtime is missing, install it from Microsoft and launch MovieMuseWorker.exe again.

The BAT files are retained only for command-line troubleshooting.
"@ | Set-Content -LiteralPath (Join-Path $packageRoot "README-WINDOWS-WORKER.txt") -Encoding UTF8
Copy-Item -LiteralPath (Join-Path $repoRoot "deploy\windows-backend\worker-release.json") -Destination (Join-Path $packageRoot "WORKER-RELEASE.json") -Force

$commit = (git -C $repoRoot rev-parse HEAD).Trim()
$sourceDirty = if (@(git -C $repoRoot status --porcelain).Count -gt 0) { "true" } else { "false" }
$requirementsHash = (Get-FileHash -LiteralPath $requirementsFile -Algorithm SHA256).Hash.ToLowerInvariant()
$gpuRequirementsHash = (Get-FileHash -LiteralPath $gpuRequirementsFile -Algorithm SHA256).Hash.ToLowerInvariant()
@"
version=$Version
source_commit=$commit
source_dirty=$sourceDirty
python=$PythonVersion
requirements_sha256=$requirementsHash
gpu_requirements_sha256=$gpuRequirementsHash
package_flavor=lightweight-online
desktop_launcher=MovieMuseWorker.exe
desktop_runtime=WebView2
"@ | Set-Content -LiteralPath (Join-Path $packageRoot "BUILD-INFO.txt") -Encoding ASCII

$forbidden = @(Get-ChildItem -LiteralPath $packageRoot -Recurse -File -Force | Where-Object {
    $_.Name -match '(?i)^(system_settings\.json|initial_admin_password\.txt|model\.bin)$' -or
    $_.Name -match '(?i)\.(sqlite3(?:-.+)?|db|log|env|safetensors|ckpt)$'
})
if ($forbidden.Count -gt 0) {
    $names = ($forbidden | ForEach-Object { $_.FullName.Substring($packageRoot.Length + 1) }) -join ', '
    throw "Forbidden runtime data was found in the release package: $names"
}

$releaseDataPath = Join-Path $packageRoot "data"
if (Test-Path -LiteralPath $releaseDataPath) {
    $dataFiles = @(Get-ChildItem -LiteralPath $releaseDataPath -Recurse -File -Force)
    if ($dataFiles.Count -gt 0) {
        throw "The release package must not contain mutable Worker data."
    }
}

$validationRoot = Join-Path $outputRoot ".tmp-worker-release-validation-$([Guid]::NewGuid().ToString('N'))"
Assert-ReleaseTarget $validationRoot
New-Item -ItemType Directory -Path $validationRoot -Force | Out-Null
$previousAppDataDir = [Environment]::GetEnvironmentVariable("APP_DATA_DIR", "Process")
$previousComputeNodeOnly = [Environment]::GetEnvironmentVariable("COMPUTE_NODE_ONLY", "Process")
$previousAdminPassword = [Environment]::GetEnvironmentVariable("MOVIEMUSE_ADMIN_PASSWORD", "Process")
try {
    Push-Location $packageRoot
    try {
        $env:APP_DATA_DIR = $validationRoot
        $env:COMPUTE_NODE_ONLY = "1"
        $env:MOVIEMUSE_ADMIN_PASSWORD = "release-validation-only"
        & (Join-Path $pythonRoot "python.exe") -c "import fastapi, faster_whisper, uvicorn; from app.main import app; print('worker_import_check=ok')"
        if ($LASTEXITCODE -ne 0) {
            throw "Windows worker import check failed with exit code $LASTEXITCODE."
        }
        if (-not (Test-Path -LiteralPath (Join-Path $packageRoot "worker-ui\index.html") -PathType Leaf)) {
            throw "Worker UI is missing from the release package."
        }
        if (Test-Path -LiteralPath (Join-Path $sitePackages "nvidia")) {
            throw "The lightweight release unexpectedly contains bundled NVIDIA runtime libraries."
        }
    } finally {
        [Environment]::SetEnvironmentVariable("APP_DATA_DIR", $previousAppDataDir, "Process")
        [Environment]::SetEnvironmentVariable("COMPUTE_NODE_ONLY", $previousComputeNodeOnly, "Process")
        [Environment]::SetEnvironmentVariable("MOVIEMUSE_ADMIN_PASSWORD", $previousAdminPassword, "Process")
        Pop-Location
    }
} finally {
    if (Test-Path -LiteralPath $validationRoot) {
        Remove-Item -LiteralPath $validationRoot -Recurse -Force
    }
}

Write-Host "Creating release archive..."
& tar.exe -a -cf $archivePath -C $outputRoot $packageName
if ($LASTEXITCODE -ne 0) {
    throw "Archive creation failed with exit code $LASTEXITCODE."
}

$archive = Get-Item -LiteralPath $archivePath
$archiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Host "Package: $packageRoot"
Write-Host "Archive: $archivePath"
Write-Host "Size: $($archive.Length) bytes"
Write-Host "SHA256: $archiveHash"
