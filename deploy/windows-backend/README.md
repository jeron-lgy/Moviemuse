# Windows Backend

This machine runs Whisper on the local GPU and exposes the API used by the Unraid frontend.

## Start

The portable release uses a native desktop window. Extract the full zip and double-click:

```text
MovieMuseWorker.exe
```

The launcher silently starts the local FastAPI worker and hosts the existing Vue Worker UI in Microsoft Edge WebView2. It does not open a command window or an external browser. Closing the desktop window also stops the backend process started by that window.

If a compatible MovieMuse Worker is already listening on port 18181, the desktop window connects to it instead of attempting a duplicate start. If another application owns the port, the desktop window shows a readable error page.

For source-tree development only, double click from the project root:

```text
start_windows_backend.bat
```

Or double click:

```text
deploy\windows-backend\start_backend.bat
```

Manual PowerShell equivalent:

```powershell
$env:WHISPER_MODEL='large-v3-turbo'
$env:WHISPER_MODEL_DIR='data\local-backend\whisper-models'
$env:WHISPER_DEVICE='cuda'
$env:WHISPER_COMPUTE_TYPE='float16'
$env:SUBTITLE_PATH_MAP=''
.\start_local_backend.bat
```

`start_windows_backend.bat` will create `.venv` and install `requirements.txt` automatically when dependencies are missing.

The release contains no Whisper model weights. Models can be downloaded and verified from the desktop model page after the Worker starts.

Starting with v2.3.0, the default release is a lightweight online package. It also leaves the large NVIDIA cuBLAS and cuDNN runtime libraries out of the zip. The model page detects compatible CUDA 12 / cuDNN 9 DLLs already available to Windows and reuses them. If they are missing, click **安装运行环境** once; the Worker downloads about 1.2 GB into `%LOCALAPPDATA%\MovieMuse Worker\gpu-runtime` and asks you to restart the desktop app. Models, GPU libraries, logs, and settings now live outside the release folder and survive upgrades. Existing `data\local-backend` content is copied into the fixed location once on first launch.

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
http://127.0.0.1:18181/worker
```

The Worker UI shows local hardware, task activity, and Whisper model files. Compute settings are read-only here and remain controlled by MovieMuse.

The top-bar compute switch starts enabled on every Worker launch. Turning it off keeps the management UI and model maintenance available, lets current jobs finish, and rejects new Whisper, translation, and transcode jobs until it is turned on again.

Model actions in the Worker UI are real local operations:

- Download and repair write into a temporary directory first, verify the required faster-whisper files, and only then install the model.
- Pause, resume, and cancel operate on the current network stream. Partial files are retained for resume and unfinished jobs return as paused after a Worker restart.
- Update is available only for inactive models. The model selected by MovieMuse must be switched in the controller before it can be replaced or removed.
- Download speed, remaining time, current file, failures, retry, verification, local folder opening, and diagnostics are reported by the Worker API.

The model page also detects NVIDIA GPU memory and recommends a matching Whisper model without changing the controller-selected model. Installed models compare their local Hugging Face revision with the latest repository revision. The overview compares the packaged `BUILD-INFO.txt` version with the dedicated Worker release manifest. Results are cached for six hours, and failed network checks never prevent local compute work.

The downloader honors the standard Hugging Face environment variables `HF_ENDPOINT` and `HF_TOKEN`. For example, a trusted mirror can be configured before startup with:

```powershell
$env:HF_ENDPOINT='https://your-huggingface-mirror.example'
```

Software version checks use the dedicated MovieMuse Worker JSON manifest by default. A different manifest with `version` (or `latest_version`) and optional `release_url` can be configured with `MOVIEMUSE_WORKER_UPDATE_URL`; private repositories can use `MOVIEMUSE_WORKER_UPDATE_TOKEN` or `GITHUB_TOKEN`.

Whisper models are kept in a dedicated folder so they can be downloaded or replaced without touching app files:

```text
%LOCALAPPDATA%\MovieMuse Worker\whisper-models
```

Useful links:

- https://github.com/SYSTRAN/faster-whisper
- https://huggingface.co/Systran/faster-whisper-large-v3
- https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo

## Media path boundary

The worker only reads and writes files under the Windows targets configured by the console path map. To lock the worker to locally approved roots regardless of remote settings, set a semicolon-separated environment variable before startup:

```powershell
$env:COMPUTE_ALLOWED_MEDIA_DIRS='\\NAS\media'
```

Uploaded videos are limited to 8 GiB by default. Override `SUBTITLE_UPLOAD_MAX_BYTES` only when larger direct uploads are required.

## Build the portable release

From the repository root on Windows with Python 3.13 available for the desktop-shell build:

```powershell
.\deploy\windows-backend\build_release.ps1 -Version v2.2.1
```

The script creates `MovieMuseWorker.exe`, a clean portable Python runtime, and a zip under `dist/`. It copies only tracked application files and rejects SQLite databases, settings, logs, credentials, and Whisper model files.
