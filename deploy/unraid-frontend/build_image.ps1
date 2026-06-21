$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..\..")
$dockerfile = Join-Path $scriptDir "Dockerfile"
$tarPath = Join-Path $scriptDir "moviemuse-unraid.tar"

$docker = "docker"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  $dockerPath = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
  if (-not (Test-Path $dockerPath)) {
    throw "Docker CLI was not found. Start Docker Desktop or install Docker Desktop first."
  }
  $docker = $dockerPath
}

Write-Host "Checking Docker Engine..."
& $docker info | Out-Host

Write-Host "Building moviemuse-unraid:latest..."
& $docker build -t moviemuse-unraid:latest -f $dockerfile $projectRoot

Write-Host "Saving image to $tarPath..."
& $docker save -o $tarPath moviemuse-unraid:latest

Write-Host ""
Write-Host "Done."
Write-Host "Copy this folder to Unraid or copy only the tar:"
Write-Host "  $tarPath"
