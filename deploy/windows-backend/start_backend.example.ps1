$env:WHISPER_MODEL = "large-v3"
$env:WHISPER_MODEL_DIR = "$PSScriptRoot\..\..\data\local-backend\whisper-models"
$env:WHISPER_DEVICE = "cuda"
$env:WHISPER_COMPUTE_TYPE = "float16"
$env:SUBTITLE_MAX_WORKERS = "1"
$env:HOST = "0.0.0.0"
$env:SUBTITLE_PATH_MAP = ""
$env:COMPUTE_NODE_ONLY = "1"

Set-Location (Resolve-Path "$PSScriptRoot\..\..")
New-Item -ItemType Directory -Force -Path $env:WHISPER_MODEL_DIR | Out-Null
.\start_dev.bat
