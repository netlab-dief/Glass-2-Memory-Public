# G2M Stage 3: zero-byte-error exploratory analysis

This *additional* offline analyzer is intended for a specific capture with a
small number of V4L2 error-flagged buffers that all have `bytesused=0`.
It NEVER modifies the camera, driver, FPGA, LED, input CSV, or raw data, and
it leaves the original `g2m_analyze.py` untouched.

## Why this version exists

The original analyzer refuses any V4L2 buffer error. In the pilot data there
were 14 flagged buffers of 4,314, and **each error buffer had zero payload**.
Empty buffers can be skipped for decoding, but they may represent data loss.
We **cannot** treat LED transitions close to them as trustworthy.

When enabled explicitly, this analyzer:

- Rejects any error-marked buffer with nonzero data (or >2% error-marked
  buffers).
- Checks raw lengths, file offsets, monotonic timestamps, sequences, format,
  recording span, and LED-log completeness.
- Omits error-overlapping windows from automatic LED-ROI calibration. Requires
  at least one clean ON and one clean OFF calibration window.
- Labels each LED edge with `quality=excluded_near_empty_error` if an errored
  buffer occurred between `command_before - 500ms` (baseline lookback) and
  `min(command_after + window_ms, next command_before)` (detection window).
- Produces latency statistics **only for usable measurement transitions**.
- Reports counts of error buffers, excluded trials, usable trials, detected
  trials, and missing trials separately. It does **not** silently remove
  exclusions or misses from the report.
- Searches only buffers timestamped *after* completion of the LED sysfs write
  (to avoid accepting a buffer completed before the command finished).

The exclusion rule is deliberately conservative but cannot prove that
surviving trials are unaffected by upstream loss, time shifts, or trigger
mistakes. Results are **exploratory**, not publication-grade. In particular,
P95 and P99 estimates from fewer than a few dozen trials are unstable.

## Install on KV260

Transfer `kv260-g2m-stage3-errorsafe.zip` into your home directory, then:

```bash
cd ~
unzip kv260-g2m-stage3-errorsafe.zip
```

Only two *new* files are installed in `~/g2m_stage3/`.

## Analyze the existing run (no reboot or recapture)

```bash
RUN="$(cat /tmp/g2m-current-run-path)"
python3 ~/g2m_stage3/g2m_analyze_errorsafe.py "$RUN" \
  --allow-empty-error-buffers
```

Outputs under `"$RUN/analysis-errorsafe/"`:

- `results.json`: clearly labeled exploratory statistics, exclusions and ROI.
- `detected_transitions.csv`: each LED edge and exclusion-quality indicator.

If automatic ROI fails or chooses the wrong LED, manually specify a
**physically verified** 320×320 LED region, e.g. `--roi X0 Y0 X1 Y1`, and
use `--out "$RUN/analysis-errorsafe-manual"` to avoid overwriting.
Do not tune ROI or detection thresholds solely to lower the measured latency.

For a sanity check on the input files before analysis, run:

```bash
python3 ~/g2m_stage2/validate_capture.py "$RUN/camera"
```

The Stage 2 validator can return `PASS` even if buffers were flagged with
errors. Use its error count and the error-aware analysis report as well.

## How to present numbers

Label any result as *provisional host-software LED-write-completion to
V4L2-buffer DMA callback latency*. Always include the numbers of total,
excluded, missing, and detected LED edges. The measurements do **not** isolate
sensor latency and should not be reported as optical glass-to-sensor latency.
