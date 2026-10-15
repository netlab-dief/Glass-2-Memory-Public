# KV260 / GenX320 G2M recorder — Stage 2

This is a standalone **C++ V4L2 MMAP recorder** for the verified 64 KiB
PSE2/EVT21 camera configuration, not a replacement for the existing video
driver and not an FPGA configuration utility.

## What gets saved

- `events.raw` — exactly the bytes returned by V4L2, concatenated in DQBUF
  order (no frame conversion and no timestamps inserted into the stream).
- `v4l2_buffers.csv` — for *every* recorded dequeued buffer: sequence,
  MMAP buffer index, file offset, payload length, V4L2 flags, error bit,
  driver-provided timestamp, and CLOCK_MONOTONIC timestamps immediately before
  and after the DQBUF call.
- `capture_manifest.json` — tested video format, capture boundaries in
  CLOCK_MONOTONIC, byte/buffer counts, stop reason, and timestamp flags.

The current `psee-dma.c` writes `ktime_get_ns()` into
`vb2_buf.timestamp`, meaning the driver timestamp is host monotonic
time near the DMA callback. This is a software/kernel completion timestamp,
**not** an optical LED onset timestamp.

EVT21 event timestamps use the sensor's clock, so do *not* directly subtract
them from the LED controller's `CLOCK_MONOTONIC` timestamps. To map which
buffer contains a detected event, use `file_offset` + `bytesused` in the CSV.

## Build (on the KV260)

```bash
g++ -O2 -Wall -Wextra -std=c++17   -o g2m_record_v4l2 g2m_record_v4l2.cpp
./g2m_record_v4l2 --help
```

No need to rebuild, remove, or reload kernel modules.

## Before running

Confirm `~/genx320-setup-64k.sh` has already brought up the camera correctly.
Verify without querying V4L2 enumeration (which previously caused kernel
warnings):

```bash
v4l2-ctl -d /dev/video0 --get-fmt-video
v4l2-ctl -d /dev/video0   --get-ctrl=transfer_timeout_enable,transfer_timeout_threshold_us
```

The tested preview configuration uses **PSE2, 320×320, sizeimage=65536,
transfer_timeout_enable=0**. If it is not configured correctly, stop and
restore the known-good state first.

**If the current boot has shown `unexpected boot magic`, a failed
`VIDIOC_STREAMON`, `Cannot stop channel`, or a kernel fault, do not attempt
another capture until the failure is investigated or the board has been
recovered by a normal reboot.** Never hot-unload the DMA driver or cycle
the FPGA overlay to work around it.

## First test: one bounded camera capture, without LED script

Ensure you have spare disk space and no other process streaming the camera.
The tool creates its output directory; it refuses to overwrite existing runs.

```bash
./g2m_record_v4l2 --out-dir /tmp/g2m-cam-smoke-001 \
  --seconds 8 --max-mib 32 --buffers 4
python3 validate_capture.py /tmp/g2m-cam-smoke-001
```

If `STREAMON` fails, don't retry it repeatedly. Inspect `sudo dmesg |
tail -100` and stop.

`--seconds` and `--max-mib` both limit the recording. In addition, a default
256 MiB *free disk reserve* triggers shutdown when space runs low (override
with `--reserve-mib` for testing on a suitably provisioned filesystem).

The program requests `VIDIOC_STREAMOFF` once on normal completion and cleanup.
This does **not** guarantee the kernel DMA channel halts correctly; check
`dmesg` after the test given our prior observed shutdown warnings.

## Combined 2-cycle LED smoke test (after camera smoke passes)

Use separate terminal sessions. Create a new run parent directory and
remember its path:

```bash
RUN="$HOME/g2m-results/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN"
echo "$RUN"
```

**Terminal A** — start the recorder and watch for `STREAMING`:

```bash
./g2m_record_v4l2 --out-dir "$RUN/camera" \
  --seconds 16 --max-mib 64
```

**Terminal B** — replace `RUN` with the directory printed in terminal A.
Start LED control only **after** terminal A reports `STREAMING`:

```bash
sudo python3 ~/g2m_kv260/kv260_led_g2m.py \
  --run --cycles 2 --seed 42 --warmup 2 --postroll 2 \
  --out "$RUN/led"
```

The LED `--out` directory must not already exist. On both terminals make sure
the run directory path is **identical**. Let the LED script finish first.
Then wait for the bounded camera capture. The scripts never change the FPGA
design or interact with the custom sensor GPIO power/reset lines.

Keep the preview closed during acquisition. Validate the camera output with:

```bash
python3 validate_capture.py "$RUN/camera"
sudo dmesg | grep -E 'Cannot stop channel|unexpected boot magic|Kernel panic' | tail -15
```

If successful, the next stage is to detect CD_ON/CD_OFF bursts within a
predefined ROI, match them to the LED CSV commands, and locate the V4L2 buffer
that first contains the burst. Use command-to-DMA-completion as an **end-to-end
software-referenced** metric, not intrinsic photon-to-event latency.

## Reproducibility and limitations

- Never rely on OpenCV preview output for the benchmark.
- Log driver `.ko` hashes, kernel version, active FPGA application, stimulus
  script hash, fixed LED ROI, baseline event rate, and packetizer controls in
  a separate run manifest before taking final results.
- This recorder does not change sensor configuration, GPIO state, or FPGA.
- This is a *new untested capture tool on the physical KV260*; compiling on a
  different Linux computer only checks source compatibility.
- A failed stream-off may leave a DMA channel unhealthy; inspect logs and do
  not begin further captures in that boot.
