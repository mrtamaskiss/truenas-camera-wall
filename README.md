# TrueNAS Camera Wall

Dockerized FFmpeg camera wall for TrueNAS SCALE custom apps.

It reads multiple RTSP or HTTP camera inputs, renders them into one H.264 wall stream, and publishes that stream to an existing go2rtc RTSP ingest endpoint. It includes a small password-protected admin UI for editing cameras, output settings, and layout without SSH.

## Architecture

- `camera_wall.config` loads YAML, resolves environment variables and Docker secrets, and validates the wall layout.
- `camera_wall.ffmpeg` builds one FFmpeg command with a generated `filter_complex` graph.
- `camera_wall.input_health` tracks enabled, disabled, active, restarting, and failed camera states for the admin UI.
- `camera_wall.gpu` samples Intel GPU utilization with `intel_gpu_top` when the host exposes the required counters.
- `camera_wall.diagnostics` probes camera streams, output targets, and local GPU metric access for the admin UI.
- `camera_wall.workers` optionally starts one lightweight FFmpeg remux worker per camera and publishes fixed intermediate RTSP slots.
- `camera_wall.supervisor` preflights inputs, starts FFmpeg, logs a credential-masked command, and restarts the pipeline when FFmpeg exits, a camera recovers, or the admin UI applies a new config.
- `camera_wall.web` serves the admin UI and writes the private YAML config mounted at `/config/config.yaml`.
- Docker healthcheck reports healthy only while the supervised FFmpeg process is alive.

The wall uses a black 1920x1080 canvas by default. Each enabled input is scaled with `force_original_aspect_ratio=decrease`, padded to its configured cell, and overlaid at its configured position. Inputs are never stretched unless `preserve_aspect: false` is explicitly set. Each tile has a black offline placeholder under the live camera layer.

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

go2rtc is not replaced by this app. In the recommended setup, go2rtc remains the streaming broker: cameras can be normalized behind go2rtc URLs, the camera-wall app publishes one composed wall stream back to go2rtc, and viewers connect to go2rtc instead of the compositor container.

If remux workers are enabled without a custom template, add one empty go2rtc stream per camera slot too:

```yaml
streams:
  camera_wall:
  camera_wall_camera-1:
  camera_wall_camera-2:
  camera_wall_camera-3:
```

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
- optional remux worker settings
- layout templates: auto, 3 wall, 2x2, 5 wall, grid, focus
- current status, per-camera input health, Intel GPU load, recent logs, masked FFmpeg command, and config download
- diagnostics for individual camera streams, the go2rtc output target, and Intel GPU metric permissions

Saving in the UI validates the full config, writes `/config/config.yaml`, and restarts only the supervised FFmpeg process. If your current config uses `${CAMERA_1_URL}` style environment variables, saving from the UI writes the resolved URL into the private config file.

The Status panel shows the supervised FFmpeg process state, PID, restart count, encoder, input decode mode, output URL, per-camera input health, GPU load when available, the masked generated FFmpeg command, and an in-memory log tail. Set `CAMERA_WALL_LOG_BUFFER_LINES` to change the retained log line count.

## Diagnostics

The admin UI includes checks that run without saving the config:

- Camera `Test` runs `ffprobe` against the current RTSP/HTTP URL and reports codec, resolution, FPS, pixel format, audio presence, timeout, auth, and connect errors.
- `Test Output` checks that the RTSP output host and port are reachable, which is useful for confirming go2rtc is listening on `8554`.
- `Test GPU` checks `/dev/dri`, the configured render device, `intel_gpu_top`, and whether GPU load counters are readable.

Diagnostic results mask credentials before returning data to the browser.

## Input Isolation

Input preflight is enabled by default. Before starting FFmpeg, the supervisor probes every enabled camera. Streams that pass the probe are included as real FFmpeg inputs. Streams that fail are omitted from the input list, but their tile remains on the wall as an offline placeholder.

If an omitted camera later recovers, the supervisor requests a controlled FFmpeg restart so the recovered stream can join the wall.

Environment variables:

```text
CAMERA_WALL_INPUT_PREFLIGHT_ENABLED=true
CAMERA_WALL_INPUT_PREFLIGHT_TIMEOUT=5
CAMERA_WALL_INPUT_REPROBE_SECONDS=30
```

This is practical isolation for startup and recovery failures while keeping one compositor process. go2rtc is still the preferred ingest/fanout layer.

## Remux Workers

Remux worker mode is optional and experimental. It starts a separate FFmpeg process for each enabled camera:

```text
camera -> per-camera FFmpeg worker with -c:v copy -> go2rtc worker slot -> wall FFmpeg -> go2rtc camera_wall
```

The worker normally does not decode or encode video, so the extra CPU/GPU load should be small. It does add one extra local RTSP publish/read path per camera. The main benefit is that each camera connection can restart independently while the wall keeps reading stable slot URLs.

Enable it in YAML or from the admin UI:

```yaml
workers:
  enabled: true
  mode: remux
  output_template: ""
  rtsp_transport: tcp
  restart_delay_seconds: 5
  start_grace_seconds: 2
  wall_input_preflight: false
```

With the default empty `output_template`, the app derives worker output URLs from the final wall URL:

```text
rtsp://192.168.64.10:8554/camera_wall_camera-1
rtsp://192.168.64.10:8554/camera_wall_camera-2
rtsp://192.168.64.10:8554/camera_wall_camera-3
```

You can override this with a template:

```yaml
workers:
  enabled: true
  output_template: rtsp://192.168.64.10:8554/wall_slot_{index}_{name}
```

`{index}` is 1-based, and `{name}` is a URL-safe version of the camera name. In go2rtc, every generated worker stream must exist as an empty stream before the worker publishes to it.

This mode is intentionally remux-only. If a camera emits a codec that cannot be published by FFmpeg's RTSP muxer with `-c:v copy`, keep worker mode off for now. A future transcode worker mode would be heavier because it would decode and encode every camera before the wall compositor.

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

## GPU Metrics

The image includes `intel_gpu_top` through the `intel-gpu-tools` package. When `/dev/dri` and the required Intel PMU/perf counters are available inside the container, the Status panel shows:

- total GPU load
- video engine load
- render engine load
- blitter/copy load
- current GPU frequency when reported

Environment variables:

```text
CAMERA_WALL_GPU_STATS_ENABLED=true
CAMERA_WALL_GPU_DEVICE=/dev/dri/renderD128
CAMERA_WALL_GPU_SAMPLE_SECONDS=5
```

If GPU load shows `unavailable`, video processing can still be using VAAPI. It usually means the container cannot read Intel performance counters. Depending on the TrueNAS host and kernel settings, `intel_gpu_top` may need root-equivalent container permissions, `CAP_PERFMON`, `CAP_SYS_ADMIN`, or a lower host `perf_event_paranoid` value.

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
docker build -t ghcr.io/YOUR_GITHUB_USER/truenas-camera-wall:0.8.0 .
docker push ghcr.io/YOUR_GITHUB_USER/truenas-camera-wall:0.8.0
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

If you enable remux workers, add the generated worker slots here too, for example `camera_wall_camera-1`, `camera_wall_camera-2`, and `camera_wall_camera-3`.

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
    image: ghcr.io/mrtamaskiss/truenas-camera-wall:0.8.0
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
      CAMERA_WALL_GPU_STATS_ENABLED: "true"
      CAMERA_WALL_GPU_DEVICE: /dev/dri/renderD128
      CAMERA_WALL_GPU_SAMPLE_SECONDS: "5"
      CAMERA_WALL_INPUT_PREFLIGHT_ENABLED: "true"
      CAMERA_WALL_INPUT_PREFLIGHT_TIMEOUT: "5"
      CAMERA_WALL_INPUT_REPROBE_SECONDS: "30"
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

If the Status panel shows GPU load as unavailable but VAAPI encode works, enable privileged mode in the TrueNAS custom app or add the equivalent perf counter capability settings if your TrueNAS UI exposes them. Keep the app LAN-only when using elevated container permissions.

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
- Input preflight prevents startup failure of one RTSP input from blocking the whole wall. Runtime failures can still cause FFmpeg to exit, but the next supervisor cycle re-probes inputs and can publish the wall without the failed camera.
- Remux workers reduce the need to restart the wall when a camera connection restarts, but the final behavior depends on how go2rtc keeps subscriber sessions alive when a worker publisher disconnects.
- Stream diagnostics use `ffprobe` with a Python-side timeout. A passing probe confirms that FFmpeg can read the camera stream, but it does not guarantee the long-running wall pipeline will never reconnect later.
- VAAPI uses CQP by default for broad Intel driver compatibility. In this mode `output.bitrate` is only a soft configuration value for non-VAAPI encoders; use `output.vaapi_qp` to tune VAAPI quality.
- VAAPI input decode can lower CPU use, but it still downloads frames before the CPU filter graph. It is intentionally optional because camera codecs and Intel driver support vary.
- GPU load is sampled with `intel_gpu_top`; unsupported metrics or missing perf counter permissions are reported as unavailable instead of failing the service.
- Hardware acceleration depends on the host kernel, `/dev/dri`, driver support, and the FFmpeg build. Use `software` as the baseline.

## References

- TrueNAS SCALE Custom App Screens: https://www.truenas.com/docs/scale/26/apps/installcustomappscreens/
- go2rtc streaming ingest and codecs: https://go2rtc.org/
- go2rtc streams examples: https://go2rtc.org/internal/streams/
- go2rtc hardware notes: https://go2rtc.org/internal/ffmpeg/hardware/
- intel_gpu_top manual: https://manpages.debian.org/bookworm/intel-gpu-tools/intel_gpu_top.1.en.html

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
