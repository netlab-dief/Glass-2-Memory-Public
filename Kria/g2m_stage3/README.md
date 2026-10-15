# Stage 3 — produce first KV260 GenX320 G2M latency numbers

This package is an **offline analyzer**. It does not change FPGA, kernel modules,
V4L2 controls, LED trigger or the hardware. It consumes the Stage 1 LED CSV and
Stage 2 raw EVT21 + V4L2 buffer CSV.

**Definition**: time from the end of the Linux sysfs brightness write to the
V4L2 monotonic timestamp of the **first buffer** with a thresholded
polarity-specific burst of EVT21 activity in the LED ROI. This is
**software-command-to-qualifying-buffer delivery latency**, not intrinsic
photon-to-sensor, glass-to-sensor, or sensor's own event timestamp latency.
It includes Linux LED-command actuation, LED optics, sensor response,
packetizer buffer-fill, AXI DMA and its callback. No clock synchronization is
needed for those two monotonic host timestamps; the EVT21 sensor clock is not
subtracted from them.

## Prerequisite

- Working experimental `genx320-dma-stop-lab` driver, selected via **your**
  manually run setup script after a normal reboot. Do not unload FPGA/apps or
  hot-swap DMA modules.
- Your preview has already confirmed the configuration works. For a benchmark
  run, start from a **fresh boot**, run your experimental setup script manually,
  and go straight to the recorder: **do not open the preview first**, because
  ending that stream may trigger the currently unresolved restart problem.
  Never run a preview and recorder at the same time.
- Already-installed Stage 1 LED controller in `~/g2m_kv260/kv260_led_g2m.py`
- Already-compiled Stage 2 capture tool `~/g2m_stage2/g2m_record_v4l2`
- `python3 -c 'import numpy'` works (used by your fast preview too).
- On a fresh reboot if the prior recording produced 0 buffers or the camera
  failed to stream. One recorded acquisition per reboot is acceptable while
  the restart bug remains open. **Do not hot-reload the driver**.

## FIRST RUN: 12 ON/OFF cycles, with first 3 reserved for auto-ROI calibration

Duration of Stage 1 LED schedule is <= 42 s with `--cycles 12`, min/max
intervals 1.0/1.5 s, warmup 3 s, postroll 3 s. The recorder is limited to 60 s
and 1024 MiB. Verify disk has >= 1.5 GiB free and some safety margin before
starting; recorder stops if free space falls below 256 MiB.

**Terminal A**:

```bash
mkdir -p "$HOME/g2m-results"
RUN="$HOME/g2m-results/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN"
printf '%s\n' "$RUN" > /tmp/g2m-current-run-path
printf 'Run directory: %s\n' "$RUN"
df -h "$HOME"
sha256sum ~/genx320-dma-stop-lab/psee-video.ko \
           ~/g2m_stage2/g2m_record_v4l2 \
           ~/g2m_kv260/kv260_led_g2m.py > "$RUN/code_hashes.txt"
uname -a > "$RUN/kernel.txt"
v4l2-ctl -d /dev/video0 --get-fmt-video > "$RUN/video_format.txt"
v4l2-ctl -d /dev/video0 --get-ctrl=transfer_timeout_enable,transfer_timeout_threshold_us \
  > "$RUN/video_controls.txt"
~/g2m_stage2/g2m_record_v4l2 --out-dir "$RUN/camera" \
  --seconds 60 --max-mib 1024 --buffers 4
```

Watch for the literal `STREAMING.` line before starting Terminal B. On the
experimental driver, a camera recording may be only successful once after a
reboot; DO NOT repeat immediately if no buffers were captured.

**Terminal B**, **after** Terminal A prints `STREAMING.`:

```bash
RUN="$(cat /tmp/g2m-current-run-path)"
sudo python3 ~/g2m_kv260/kv260_led_g2m.py \
  --run --cycles 12 --seed 42 \
  --warmup 3 --postroll 3 \
  --out "$RUN/led"
```

Leave Terminal A running to its planned end, even after LED stimulation ends.
The `--max-mib` limit should not be reached; if it is, the analyzer **rejects**
that run rather than presenting partial data as complete. For long runs, allow
ample space. If the LED script errors, don't reuse the recording.

## Analysis (after both tools finish)

Install this package and run:

```bash
cd ~/g2m_stage3
RUN="$(cat /tmp/g2m-current-run-path)"
python3 g2m_analyze.py "$RUN"
cat "$RUN/analysis/results.json"
```

Outputs:

- `analysis/results.json`: ROI, selection candidates, detection count, median,
  p95/p99 and ON/OFF distributions for measured edges.
- `analysis/detected_transitions.csv`: one row per LED transition, including
  calibration vs measurement status, baseline, threshold, matched V4L2 buffer,
  and command-to-delivery latency.

Calibration cycle count defaults to 3. Only subsequent cycles contribute to
reported latency numbers. The auto ROI ranks 16x16 tiles using ON-positive
and OFF-negative event contrast and picks a 32x32 ROI centered near the best
tile; **verify that these coordinates agree with the observed heartbeat LED**.
A noisy background can produce a false ROI. The `--roi` flag allows fixed
ROI coordinates (`x0 y0 x1 y1` with exclusive upper bounds), e.g.:

```bash
python3 g2m_analyze.py "$RUN" --roi 100 80 132 112 \
  --out "$RUN/analysis-manual-roi"
```

The `--out` option creates a separate directory rather than overwriting
existing results. A manually chosen ROI should ideally be determined from a
separate calibration recording, not selected to maximize these measurements.

## What to report

- **Definition**: *LED software brightness write completed -> first
  polarity-qualifying V4L2 DMA buffer callback timestamp*.
- ROI and detection threshold, packet size, and packetizer timeout control.
- Number of measured edges, detection misses, ON/OFF median, p95 (and p99
  only when enough independent samples).
- Exact `psee-video.ko` SHA, kernel and active FPGA application.
- Caveats: output is a host/software-referenced end-to-end measure. It does
  **not** use sensor event clock, and sysfs timing is not physical light onset.
  DMA buffering adds variable latency; event onset may occur within a buffer
  before the command time, so a response detection is threshold/statistical,
  not an optical trigger.

For a 12-cycle run with three calibration cycles, there are only 18 measured
edges. Such a run is appropriate to establish feasibility, not a stable p99
estimate. Once successful, schedule 100 cycles for stronger percentiles and
allocate ample disk space (~1-3 GiB or more depending on event rate). Given the
restart issue, reboot **between** whole runs if necessary rather than trying
to reopen the V4L2 device repeatedly within a single boot.

Stop and inspect `sudo dmesg | tail -100` if a recording has zero buffers or
kernel errors. Do not reload the FPGA or camera drivers to clear a bad state.
