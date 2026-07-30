# TrueNAS Camera Wall

Dockerized FFmpeg camera wall for TrueNAS SCALE custom apps.

It reads multiple RTSP or HTTP camera inputs, renders them into one H.264 wall stream, and publishes that stream to an existing go2rtc RTSP ingest endpoint. The first version is intentionally small: no recording, no web UI, one FFmpeg pipeline supervised by a restart loop.

## Architecture

- `camera_wall.config` loads YAML, resolves environment variables and Docker secrets, and validates the wall layout.
- `camera_wall.ffmpeg` builds one FFmpeg command with a generated `filter_complex` graph.
- `camera_wall.supervisor` starts FFmpeg, logs a credential-masked command, and restarts the pipeline when FFmpeg exits.
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

## Encoder Selection

Set `output.encoder` or `CAMERA_WALL_ENCODER` to one of:

- `software`: `libx264`, works everywhere, uses CPU.
- `vaapi`: `h264_vaapi`, uses Intel VAAPI encode via `/dev/dri`.
- `qsv`: `h264_qsv`, uses Intel Quick Sync via `/dev/dri`.

VAAPI defaults to constant-quality CQP mode because some TrueNAS Intel drivers only report CQP support. Adjust `output.vaapi_qp` or `CAMERA_WALL_VAAPI_QP`; lower values improve quality and increase bitrate. If your driver supports bitrate control, set `output.vaapi_rc_mode` to `cbr`, `vbr`, or `auto`.

For Intel hardware acceleration in Docker or TrueNAS, pass `/dev/dri` into the container. If hardware initialization fails, switch back to `software` first to confirm the camera URLs and go2rtc ingest are working.

## Credentials

Never put real passwords in `config.example.yaml` or commit them to git.

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
docker build -t ghcr.io/YOUR_GITHUB_USER/truenas-camera-wall:0.1.3 .
docker push ghcr.io/YOUR_GITHUB_USER/truenas-camera-wall:0.1.3
```

2. On TrueNAS, create a dataset for the app config, for example:

```text
/mnt/tank/apps/camera-wall
```

3. Save your private config as:

```text
/mnt/tank/apps/camera-wall/config.yaml
```

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

9. Paste this Compose YAML, replacing image, config path, and URLs:

```yaml
services:
  camera-wall:
    image: ghcr.io/YOUR_GITHUB_USER/truenas-camera-wall:0.1.3
    restart: unless-stopped
    environment:
      CAMERA_WALL_CONFIG: /config/config.yaml
      OUTPUT_URL: rtsp://192.168.64.10:8554/camera_wall
      CAMERA_WALL_BITRATE: 5M
      CAMERA_WALL_ENCODER: software
      CAMERA_WALL_VAAPI_QP: "23"
      CAMERA_1_URL: rtsp://USER:PASSWORD@192.168.64.21:554/stream1
      CAMERA_2_URL: rtsp://USER:PASSWORD@192.168.64.22:554/stream1
      CAMERA_3_URL: rtsp://USER:PASSWORD@192.168.64.23:554/stream1
    volumes:
      - /mnt/tank/apps/camera-wall/config.yaml:/config/config.yaml:ro
```

For Intel VAAPI/QSV, add `devices` beside `volumes` and change the existing `CAMERA_WALL_ENCODER` value:

```yaml
    devices:
      - /dev/dri:/dev/dri
```

Use `CAMERA_WALL_ENCODER: vaapi` or `CAMERA_WALL_ENCODER: qsv` in the environment block.

If you use the guided Custom App wizard instead of YAML, set the same image, environment variables, read-only config mount, restart policy, and GPU passthrough. TrueNAS shows non-NVIDIA GPU passthrough under the app resource settings when hardware is detected.

10. Click `Save`.

11. Check logs. A healthy startup logs a masked FFmpeg command and `FFmpeg started with pid ...`.

12. Test from another machine:

```sh
ffplay rtsp://192.168.64.10:8554/camera_wall
```

## Compatibility Notes

- go2rtc RTSP ingest supports incoming RTSP, but the target stream must exist first with an empty source.
- Browser compatibility is simplest with H.264 video. This app encodes `yuv420p` for software mode and `nv12` for hardware encoders.
- RTSP input is forced to TCP by default for camera stability.
- FFmpeg reconnect options are strongest for HTTP inputs. For RTSP, the supervisor restarts the whole pipeline after FFmpeg exits. `ffmpeg.input_timeout_seconds` is disabled by default because some FFmpeg builds reject `-rw_timeout`.
- VAAPI uses CQP by default for broad Intel driver compatibility. In this mode `output.bitrate` is only a soft configuration value for non-VAAPI encoders; use `output.vaapi_qp` to tune VAAPI quality.
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
