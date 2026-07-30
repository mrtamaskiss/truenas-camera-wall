# TrueNAS Camera Wall

Dockerized FFmpeg camera wall for TrueNAS SCALE custom apps.

It reads multiple RTSP or HTTP camera inputs, renders them into one H.264 wall stream, and publishes that stream to an existing go2rtc RTSP ingest endpoint. It includes a small password-protected admin UI for editing cameras, output settings, and layout without SSH.

## Architecture

- `camera_wall.config` loads YAML, resolves environment variables and Docker secrets, and validates the wall layout.
- `camera_wall.ffmpeg` builds one FFmpeg command with a generated `filter_complex` graph.
- `camera_wall.supervisor` starts FFmpeg, logs a credential-masked command, and restarts the pipeline when FFmpeg exits or the admin UI applies a new config.
- `camera_wall.web` serves the admin UI and writes the private YAML config mounted at `/config/config.yaml`.
- Docker healthcheck reports healthy only while the supervised FFmpeg process is alive.

The wall uses a black 1920x1080 canvas by default. Each enabled input is scaled with `force_original_aspect_ratio=decrease`, padded to its configured cell, and overlaid at its configured position. Inputs are never stretched unless `preserve_aspect: false` is explicitly set.

## Default Layout

| Input | Position | Size |
| --- | --- | --- |
| Camera 1 | top left | 960x540 |
| Camera 2 | top right | 960x540 |
| Camera 3 | bottom | 1920x540 |

Output defaults:

- 1920x1080
- 15 FPS
- H.264
- 5 Mbps
- RTSP publish URL: `rtsp://192.168.64.10:8554/camera_wall`

## go2rtc Setup

go2rtc RTSP ingest expects the destination stream to already exist. Add an empty stream to your go2rtc config:

```yaml
streams:
  camera_wall:
```

Then publish to:

```text
rtsp://192.168.64.10:8554/camera_wall
```

This project sends video only. If a player path requires audio later, add AAC audio in go2rtc or extend the FFmpeg command.

## Local Docker Compose

1. Copy the examples:

```sh
cp .env.example .env
cp config.example.yaml config.yaml
```

2. Edit `.env` and set real camera URLs. Do not commit `.env`.

3. Start the service:

```sh
docker compose up --build
```

4. Open the admin UI:

```text
http://localhost:8088
```

Use `CAMERA_WALL_ADMIN_USER` and `CAMERA_WALL_ADMIN_PASSWORD` from `.env`.

## Admin UI

The admin UI can edit:

- RTSP or HTTP camera URLs
- camera names, labels, enabled state, and cell positions
- output URL, resolution, FPS, bitrate, encoder, input decode, VAAPI/QSV options
- layout templates: auto, 3 wall, grid, focus
- current status, recent logs, masked FFmpeg command, and config download

Saving in the UI validates the full config, writes `/config/config.yaml`, and restarts only the supervised FFmpeg process. If your current config uses `${CAMERA_1_URL}` style environment variables, saving from the UI writes the resolved URL into the private config file.

The Status panel shows the supervised FFmpeg process state, PID, restart count, encoder, input decode mode, output URL, the masked generated FFmpeg command, and an in-memory log tail. Set `CAMERA_WALL_LOG_BUFFER_LINES` to change the retained log line count.

## Encoder Selection

Set `output.encoder` or `CAMERA_WALL_ENCODER` to one of:

- `software`: `libx264`, works everywhere, uses CPU.
- `vaapi`: `h264_vaapi`, uses Intel VAAPI encode via `/dev/dri`.
- `qsv`: `h264_qsv`, uses Intel Quick Sync via `/dev/dri`.

VAAPI defaults to constant-quality CQP mode because some TrueNAS Intel drivers only report CQP support. Adjust `output.vaapi_qp` or `CAMERA_WALL_VAAPI_QP`; lower values improve quality and increase bitrate. If your driver supports bitrate control, set `output.vaapi_rc_mode` to `cbr`, `vbr`, or `auto`.

For Intel hardware acceleration in Docker or TrueNAS, pass `/dev/dri` into the container. If hardware initialization fails, switch back to `software` first to confirm the camera URLs and go2rtc ingest are working.

## Input Hardware Decode

Set `ffmpeg.input_hwaccel` or `CAMERA_WALL_INPUT_HWACCEL` to:

- `software`: default, CPU decode.
- `vaapi`: VAAPI decode for each input, then `hwdownload` back to CPU filters.

The current stable pipeline still keeps scale, padding, labels, and overlay on CPU. With `vaapi` input decode, the path is:

```text
VAAPI decode -> CPU scale/pad/overlay/drawtext -> VAAPI encode
```

If a camera codec or driver rejects VAAPI decode, set `CAMERA_WALL_INPUT_HWACCEL: software` and apply again.

## Credentials

Never put real passwords in `config.example.yaml` or commit them to git.

The admin UI stores camera URLs in the mounted private config file, typically:

```text
/mnt/tank/apps/camera-wall/config.yaml
```

Protect that dataset and do not expose the admin UI outside your LAN. The UI requires HTTP Basic Auth via `CAMERA_WALL_ADMIN_USER` and `CAMERA_WALL_ADMIN_PASSWORD`.

Use environment variables:

```yaml
inputs:
  - name: camera-1
    url: ${CAMERA_1_URL}
```

Or Docker secrets:

```yaml
inputs:
  - name: camera-1
    url: ${secret:camera_1_url}
```

With Compose:

```yaml
services:
  camera-wall:
    secrets:
      - camera_1_url

secrets:
  camera_1_url:
    file: ./secrets/camera_1_url
```

The app masks credentials when it logs FFmpeg commands.

## TrueNAS SCALE Installation

These steps target TrueNAS SCALE 26 custom apps. TrueNAS documents two custom app paths: the guided Custom App wizard and the advanced `Install via YAML` editor for Docker Compose syntax. Use the YAML path for this app.

1. Build and publish the image to a registry that TrueNAS can pull, for example GHCR:

```sh
docker build -t ghcr.io/YOUR_GITHUB_USER/truenas-camera-wall:0.4.1 .
docker push ghcr.io/YOUR_GITHUB_USER/truenas-camera-wall:0.4.1
```

2. On TrueNAS, create a dataset for the app config, for example:

```text
/mnt/tank/apps/camera-wall
```

3. Optionally seed your private config as:

```text
/mnt/tank/apps/camera-wall/config.yaml
```

If the file is missing, the container stays up and lets you create it from the admin UI.

4. In go2rtc, create the empty ingest stream:

```yaml
streams:
  camera_wall:
```

5. In TrueNAS, open `Apps`.

6. Open `Discover`.

7. Use the three-dot menu and choose `Install via YAML`.

8. Set the app name to:

```text
camera-wall
```

9. Paste this Compose YAML, replacing the admin password and URLs:

```yaml
services:
  camera-wall:
    image: ghcr.io/mrtamaskiss/truenas-camera-wall:0.4.1
    container_name: camera-wall
    restart: unless-stopped
    network_mode: host
    environment:
      CAMERA_WALL_CONFIG: /config/config.yaml
      CAMERA_WALL_WEB_ENABLED: "true"
      CAMERA_WALL_WEB_HOST: 0.0.0.0
      CAMERA_WALL_WEB_PORT: "8088"
      CAMERA_WALL_ADMIN_USER: admin
      CAMERA_WALL_ADMIN_PASSWORD: change-this-password
      CAMERA_WALL_LOG_BUFFER_LINES: "500"
      OUTPUT_URL: rtsp://192.168.64.10:8554/camera_wall
      CAMERA_WALL_BITRATE: 5M
      CAMERA_WALL_ENCODER: vaapi
      CAMERA_WALL_VAAPI_QP: "23"
      CAMERA_WALL_INPUT_HWACCEL: vaapi
      CAMERA_1_URL: rtsp://192.168.64.10:8554/cam_a40ca720_1
      CAMERA_2_URL: rtsp://192.168.64.10:8554/cam_a70cabd8_1
      CAMERA_3_URL: rtsp://192.168.64.10:8554/cam_a60caa40_1
      LIBVA_DRIVER_NAME: iHD
    devices:
      - /dev/dri:/dev/dri
    volumes:
      - /mnt/tank/apps/camera-wall:/config
```

Use `CAMERA_WALL_ENCODER: vaapi` or `CAMERA_WALL_ENCODER: qsv` in the environment block.

If you use the guided Custom App wizard instead of YAML, set the same image, environment variables, writable config mount, restart policy, host networking or equivalent port access, and GPU passthrough. TrueNAS shows non-NVIDIA GPU passthrough under the app resource settings when hardware is detected.

10. Click `Save`.

11. Open the admin UI:

```text
http://TRUENAS-IP:8088
```

12. Check logs. A healthy startup logs a masked FFmpeg command and `FFmpeg started with pid ...`.

13. Test from another machine:

```sh
ffplay rtsp://192.168.64.10:8554/camera_wall
```

## Compatibility Notes

- go2rtc RTSP ingest supports incoming RTSP, but the target stream must exist first with an empty source.
- Browser compatibility is simplest with H.264 video. This app encodes `yuv420p` for software mode and `nv12` for hardware encoders.
- RTSP input is forced to TCP by default for camera stability.
- FFmpeg reconnect options are strongest for HTTP inputs. For RTSP, the supervisor restarts the whole pipeline after FFmpeg exits. `ffmpeg.input_timeout_seconds` is disabled by default because some FFmpeg builds reject `-rw_timeout`.
- VAAPI uses CQP by default for broad Intel driver compatibility. In this mode `output.bitrate` is only a soft configuration value for non-VAAPI encoders; use `output.vaapi_qp` to tune VAAPI quality.
- VAAPI input decode can lower CPU use, but it still downloads frames before the CPU filter graph. It is intentionally optional because camera codecs and Intel driver support vary.
- Hardware acceleration depends on the host kernel, `/dev/dri`, driver support, and the FFmpeg build. Use `software` as the baseline.

## References

- TrueNAS SCALE Custom App Screens: https://www.truenas.com/docs/scale/26/apps/installcustomappscreens/
- go2rtc streaming ingest and codecs: https://go2rtc.org/
- go2rtc streams examples: https://go2rtc.org/internal/streams/
- go2rtc hardware notes: https://go2rtc.org/internal/ffmpeg/hardware/

## Development

Run unit tests:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

Print the generated FFmpeg command without starting FFmpeg:

```sh
PYTHONPATH=src OUTPUT_URL=rtsp://192.168.64.10:8554/camera_wall \
CAMERA_1_URL=rtsp://user:pass@192.168.64.21/stream1 \
CAMERA_2_URL=rtsp://user:pass@192.168.64.22/stream1 \
CAMERA_3_URL=rtsp://user:pass@192.168.64.23/stream1 \
python3 -m camera_wall --config config.example.yaml --print-command
```
