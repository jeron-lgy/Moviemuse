# Windows Backend

This machine runs Whisper on the local GPU and exposes the API used by the Unraid frontend.

## Start

Double click from the project root:

```text
start_windows_backend.bat
```

Or double click:

```text
deploy\windows-backend\start_backend.bat
```

Manual PowerShell equivalent:

```powershell
$env:WHISPER_MODEL='large-v3'
$env:WHISPER_MODEL_DIR='data\local-backend\whisper-models'
$env:WHISPER_DEVICE='cuda'
$env:WHISPER_COMPUTE_TYPE='float16'
$env:SUBTITLE_PATH_MAP=''
.\start_local_backend.bat
```

`start_windows_backend.bat` will create `.venv` and install `requirements.txt` automatically when dependencies are missing.

The recommended path mapping is controlled by the Unraid console through `SUBTITLE_PROXY_PATH_MAP`, for example:

```yaml
SUBTITLE_PROXY_PATH_MAP: /media=//UNRAID/media
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:18181/health
```

Compute UI:

```text
http://127.0.0.1:18181/terminal
```

Whisper models are kept in a dedicated folder so they can be downloaded or replaced without touching app files:

```text
data\local-backend\whisper-models
```

Useful links:

- https://github.com/SYSTRAN/faster-whisper
- https://huggingface.co/Systran/faster-whisper-large-v3
- https://huggingface.co/Systran/faster-whisper-large-v3-turbo
