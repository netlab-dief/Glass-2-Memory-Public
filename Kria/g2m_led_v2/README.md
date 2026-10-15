# KV260 G2M LED Stimulus V2 — no catch-up commands

## Why this exists

The original V1 script anchored every LED deadline to one absolute start time:
`target_ns = first_edge_ns + rel_ns`. After a scheduling interruption (or a
slow `fsync`), several deadlines can already be in the past and ON/OFF commands
are then issued almost immediately, producing 4–20 ms instead of 1–1.5 s.

V2 computes the first deadline from the warm-up time, then **every subsequent
LED command target** from the previous **actual completed sysfs write** and the
next precomputed random interval. It also flushes CSV rows without forcing a
separate `fsync()` after *every* edge; it syncs the completed CSV once at the
end. It retains the original CLI, CSV schema, trigger restoration, clock,
random seed behavior, and LED-on/off write implementation. A long OS scheduling
pause may still make an interval *longer* than requested; it can no longer
make the next interval artificially short due to catch-up.

No sensor registers, camera drivers, or FPGA state are modified.

## Install on the KV260

Unzip `kv260-g2m-led-v2.zip` in the home directory. It creates `g2m_led_v2/`.
The V1 script in `~/g2m_kv260/` stays untouched.

## LED-only five-cycle smoke test — no camera, no reboot required

```bash
python3 ~/g2m_led_v2/kv260_led_g2m_v2.py --cycles 5 --warmup 1 --postroll 1
sudo python3 ~/g2m_led_v2/kv260_led_g2m_v2.py --run --cycles 5 \
  --seed 42 --min-delay 1 --max-delay 1.5 --warmup 1 --postroll 1 \
  --out /tmp/g2m-led-v2-test-001
python3 ~/g2m_led_v2/check_led_timing.py /tmp/g2m-led-v2-test-001
cat /sys/class/leds/heartbeat/trigger
```

Expected: `PASS: no compressed catch-up LED commands`. The heartbeat trigger
should be restored, shown as `[heartbeat]`. The test path must not exist before
running; choose a new name if needed.

## For the next full benchmark

Use the frozen geometry-002 analyzer unchanged. Reboot normally before the
*camera* recording because of the known DMA restart issue, run the experimental
camera setup script manually, and do **not** open a preview or run another
capture before starting the one continuous benchmark recording.

In terminal A create a new run directory as before and start the same C++
recorder. Use a generous duration bound (e.g. 420 s) so delayed stimulus
transitions finish before recording does; keep free disk and size-limit checks.
Be aware that the recorder may hit `--max-mib` or disk-reserve limits first.

In terminal B, AFTER terminal A prints `STREAMING`, launch:

```bash
RUN="$(cat /tmp/g2m-current-run-path)"
sudo python3 ~/g2m_led_v2/kv260_led_g2m_v2.py --run --cycles 100 \
  --seed 42 --min-delay 1 --max-delay 1.5 \
  --warmup 5 --postroll 5 --out "$RUN/led"
```

After LED finishes, before analyzing camera data:

```bash
python3 ~/g2m_led_v2/check_led_timing.py "$RUN/led"
python3 ~/g2m_stage2/validate_capture.py "$RUN/camera"
```

Do not publish the old 100-cycle result without reporting its compressed LED
intervals. If the recorder stalls, if the captured time does not cover the
entire LED sequence, or if the LED script was interrupted, reject the run.

## Timestamp and measurement limitations

`CLOCK_MONOTONIC` captures LED sysfs write boundaries, not optical onset.
DMA buffer timestamps represent kernel DMA completion, not individual sensor
EVT21 event time. The observed software command-to-buffer time is *not*
intrinsic sensor or photon-to-event latency.
