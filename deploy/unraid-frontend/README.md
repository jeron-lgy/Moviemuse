# Unraid Frontend

This folder contains the Docker frontend/proxy for Unraid.

It does not run Whisper locally. Subtitle jobs are forwarded to the Windows backend configured by:

```yaml
SUBTITLE_BACKEND_URL: http://WINDOWS-IP:18181
SUBTITLE_BACKEND_PUBLIC_URL: http://WINDOWS-IP:18181
```

## Run On Unraid

Copy the project root to:

```text
/mnt/user/appdata/media-toolbox
```

Then run from the project root:

```bash
mkdir -p /mnt/user/appdata/media-toolbox/data
chown -R 99:100 /mnt/user/appdata/media-toolbox/data
chmod -R u+rwX,g+rwX /mnt/user/appdata/media-toolbox/data
docker compose -f deploy/unraid-frontend/docker-compose.yml up -d --build
```

The three permission commands are required on the first Unraid installation.
The console container runs as `99:100` and needs write access to `/data` to
save the Windows worker address, translation settings, and task state.

The scan/dedupe UI will be available at:

```text
http://UNRAID_IP:18180/
```

Use these views to verify scanning:

```text
http://UNRAID_IP:18180/?view=all
http://UNRAID_IP:18180/?view=no-subtitle
http://UNRAID_IP:18180/api/scan
```

The subtitle task UI will be available at:

```text
http://UNRAID_IP:18180/subtitles
```

## Path Mapping

Inside the container, media is mounted as:

```text
/media
```

Before requests are forwarded to Windows, paths are rewritten by the console:

```text
/media -> //UNRAID/media
/trash -> //UNRAID/appdata/media-toolbox/trash
```

The Windows backend can stay simple and does not need its own path map if the console sends Windows-readable UNC paths:

```powershell
$env:SUBTITLE_PATH_MAP=''
.\start_dev.bat
```
