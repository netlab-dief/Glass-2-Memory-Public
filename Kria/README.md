# Glass2Memory - Kria KV260 Vision AI Starter Kit

Before reproducing G2M measurements with a Kriva KV260 Vision AI Starter Kit and a GenX320 sensor, check the repository of the [Ubuntu port](https://github.com/netlab-dief/kv260-genx320-ubuntu) for this configuration.

## Build C++ script for camera recording

To build the script in `g2m_stage2` run
```bash
g++ -O2 -Wall -Wextra -std=c++17 -o g2m_record_v4l2 g2m_record_v4l2.cpp
```

## Quick routine for a **new camera position**

Run commands on the **KV260**. This assumes the V4L2 recorder, error-safe analyzer, and V2 LED controller are already installed. Every new physical camera position gets a new `geometry-NNN` ID.

**1. Position the camera.** 
Connect from your PC with 

```bash
sh -X ubuntu@kria
``` 
If not yet configured in this boot, run 
```bash
sudo bash ~/genx320-setup-64k.sh
```
Set the transfer timeout to the configuration used during successful long recording:

```bash
v4l2-ctl -d /dev/video0 --set-ctrl=transfer_timeout_enable=0
```

Preview and fix the LED position:

```bash
python3 ~/genx320-port/genx320_live_fast.py
```

**2. Reboot after preview, before capturing.** Reconnect and configure the modules manually. Do **not** preview or capture again before the measurement.

```bash
sudo reboot now
# Reconnect by SSH, then:
sudo bash ~/genx320-setup-64k.sh
v4l2-ctl -d /dev/video0 --set-ctrl=transfer_timeout_enable=0
```

**3. Terminal A — create the calibration run, then start the camera.** Change `geometry-004` to a *new* identifier for your position.

```bash
GEOM="geometry-004"
mkdir -p ~/g2m-results
RUN="$HOME/g2m-results/${GEOM}-calibration-$(date +%Y%m%d-%H%M%S)"
mkdir "$RUN"
echo "$RUN" > /tmp/g2m-current-run-path
df -h "$HOME"
~/g2m_stage2/g2m_record_v4l2 --out-dir "$RUN/camera" \
  --seconds 90 --max-mib 1024 --reserve-mib 512 --buffers 4
```

**4. Terminal B — after Terminal A prints `STREAMING`, start the LED.** Do not stop the camera when LED blinking finishes; let the recorder finish its 90 seconds.

```bash
RUN="$(cat /tmp/g2m-current-run-path)"
sudo python3 ~/g2m_led_v2/kv260_led_g2m_v2.py \
  --run --cycles 12 --seed 42 --min-delay 1 --max-delay 1.5 \
  --warmup 3 --postroll 3 --out "$RUN/led"
```

**5. After both processes finish — validate and automatically discover the ROI. The first three cycles locate the LED; the next nine cycles measure it.

```bash
RUN="$(cat /tmp/g2m-current-run-path)"
python3 ~/g2m_led_v2/check_led_timing.py "$RUN/led"
python3 ~/g2m_stage2/validate_capture.py "$RUN/camera"
python3 ~/g2m_stage3/g2m_analyze_errorsafe.py "$RUN" \
  --calibration-cycles 3 --min-events 12 --window-ms 550
```

Read **`ROI: (x0, y0, x1, y1)`**, ON/OFF detection counts, median, and P95. The results are also saved at `$RUN/analysis-errorsafe/results.json` and can be analyzed with

```bash
python3 -m json.tool $RUN/analysis-errorsafe/results.json
```
The automatically selected ROI is 32×32 pixels; the upper-right coordinates are exclusive.

**Important:** Reject empty/stalled captures even when the structural validator prints `PASS`; check that camera buffer timestamps cover the LED sequence. If errors are *only isolated zero-byte buffers*, `--allow-empty-error-buffers` is an **exploratory** analysis option, not a way to certify a clean recording. Do not retry a stalled camera capture during the same boot. Never unload the FPGA application, hot-reload driver modules, or toggle sensor power/reset GPIOs. Use V2 LED timing only.

**Next:** To run **100 cycles with a fixed ROI**, it is suggested to reboot again and perform a new single camera acquisition. 

The result measures **LED sysfs write completion → V4L2 DMA buffer completion**, *not* physical optical onset or intrinsic photon-to-event latency.
