#!/usr/bin/env python3
"""Repeatable KV260 onboard heartbeat-LED stimulus for GenX320 G2M tests.

No GPIO or LED state is changed without --run. Run with sudo on the KV260.

All reported *monotonic* timestamps use time.CLOCK_MONOTONIC, the same
Linux clock domain as ktime_get_ns() used by the current psee-dma driver.
This brackets a sysfs LED command, NOT the physical optical transition.
"""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
import signal
import sys
import time

LED_DIR = Path('/sys/class/leds/heartbeat')
CSV_FIELDS = [
    'trial', 'edge', 'target_monotonic_ns',
    'before_write_monotonic_ns', 'after_write_monotonic_ns',
    'write_duration_ns', 'schedule_lateness_ns', 'utc_after_write',
]


def monotonic_ns():
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


def active_trigger(path):
    value = path.read_text().strip()
    match = re.search(r'\[([^]]+)\]', value)
    if not match:
        raise RuntimeError(f'Cannot determine current LED trigger: {value}')
    return match.group(1)


def sysfs_write(path, value):
    # File open happens before we bracket the actual write syscall.
    fd = os.open(str(path), os.O_WRONLY | os.O_CLOEXEC)
    try:
        t_before = monotonic_ns()
        payload = f'{value}\n'.encode('ascii')
        n = os.write(fd, payload)
        t_after = monotonic_ns()
        if n != len(payload):
            raise OSError(f'Short sysfs write to {path}: {n} of {len(payload)} bytes')
        return t_before, t_after
    finally:
        os.close(fd)


def sleep_until(target_ns):
    while True:
        remaining = (target_ns - monotonic_ns()) / 1e9
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.2))


def make_schedule(cycles, min_delay_s, max_delay_s, seed):
    rng = random.Random(seed)
    edges = []
    elapsed_ns = 0
    for trial in range(1, cycles + 1):
        edges.append((trial, 'ON', elapsed_ns))
        elapsed_ns += round(rng.uniform(min_delay_s, max_delay_s) * 1e9)
        edges.append((trial, 'OFF', elapsed_ns))
        elapsed_ns += round(rng.uniform(min_delay_s, max_delay_s) * 1e9)
    return edges, elapsed_ns


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true',
                        help='Actually control the onboard LED (requires root); otherwise print the plan only')
    parser.add_argument('--cycles', type=int, default=100, help='Number of ON/OFF pairs (default: 100)')
    parser.add_argument('--seed', type=int, default=42, help='Random schedule seed')
    parser.add_argument('--min-delay', type=float, default=1.0, help='Minimum ON and OFF interval in seconds')
    parser.add_argument('--max-delay', type=float, default=1.5, help='Maximum ON and OFF interval in seconds')
    parser.add_argument('--warmup', type=float, default=5.0,
                        help='Seconds to hold LED OFF before first ON (default: 5)')
    parser.add_argument('--postroll', type=float, default=5.0,
                        help='Seconds to hold LED OFF after the randomized schedule (default: 5)')
    parser.add_argument('--out', type=Path, default=Path('g2m-results/run-001'),
                        help='Output directory; must not already exist for --run')
    args = parser.parse_args()

    if args.cycles < 1 or args.warmup < 0 or args.postroll < 0 or args.min_delay <= 0 or args.max_delay < args.min_delay:
        parser.error('Require cycles>=1, warmup>=0, postroll>=0, 0<min-delay<=max-delay')
    edges, total_ns = make_schedule(args.cycles, args.min_delay, args.max_delay, args.seed)
    duration = args.warmup + total_ns / 1e9 + args.postroll

    print(f'LED: {LED_DIR}, cycles={args.cycles}, seed={args.seed}')
    print(f'Schedule duration: approximately {duration:.1f} s, including {args.warmup:g} s warm-up')
    print('First scheduled edges (relative to end of warm-up):')
    for trial, edge, rel_ns in edges[:min(6, len(edges))]:
        print(f'  trial={trial:03} edge={edge:3} t={rel_ns / 1e9:.6f} s')
    if not args.run:
        print('PLAN ONLY: No LED control or output files. Add --run to execute.')
        return 0

    if os.geteuid() != 0:
        parser.error('--run requires root; use sudo')
    if not LED_DIR.is_dir():
        parser.error(f'LED directory missing: {LED_DIR}')
    if args.out.exists():
        parser.error(f'Output path already exists; choose a new --out: {args.out}')

    trigger_path = LED_DIR / 'trigger'
    brightness_path = LED_DIR / 'brightness'
    max_brightness = int((LED_DIR / 'max_brightness').read_text().strip())
    old_trigger = active_trigger(trigger_path)
    old_brightness = int(brightness_path.read_text().strip())
    if max_brightness < 1:
        parser.error('Invalid max_brightness')

    # This script intentionally never interacts with camera control GPIOs.
    args.out.mkdir(parents=True, exist_ok=False)
    script_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    manifest = {
        'protocol': 'KV260-onboard-heartbeat-led-v2-relative',
        'schedule_mode': 'relative_to_previous_actual_write_completion',
        'status': 'in_progress',
        'clock': 'CLOCK_MONOTONIC',
        'timestamp_definition': 'before/after sysfs brightness write syscall; NOT optical LED onset',
        'led_sysfs': str(LED_DIR),
        'original_trigger': old_trigger,
        'original_brightness': old_brightness,
        'max_brightness': max_brightness,
        'cycles': args.cycles,
        'seed': args.seed,
        'min_delay_s': args.min_delay,
        'max_delay_s': args.max_delay,
        'warmup_s': args.warmup,
        'postroll_s': args.postroll,
        'script_sha256': script_sha256,
        'kernel_release': os.uname().release,
        'host': os.uname().nodename,
        'started_utc': datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = args.out / 'led_manifest.json'

    def save_manifest():
        with manifest_path.open('w') as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())

    def on_term(signum, frame):
        raise KeyboardInterrupt(f'Signal {signum}')

    previous_term = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, on_term)
    completed = 0
    armed = False
    save_manifest()
    try:
        # 'none' detaches Linux heartbeat flashing. Brightness is forced OFF.
        # In case of an error after changing trigger, the finally block restores it.
        armed = True
        sysfs_write(trigger_path, 'none')
        sysfs_write(brightness_path, '0')
        warmup_start = monotonic_ns()
        first_edge_ns = warmup_start + round(args.warmup * 1e9)
        manifest['warmup_start_monotonic_ns'] = warmup_start
        manifest['first_edge_target_monotonic_ns'] = first_edge_ns
        save_manifest()

        print('LED controlled by this script. Ensure camera recording is ALREADY running.', flush=True)
        print(f'Writing {args.out / "led_transitions.csv"}', flush=True)
        with (args.out / 'led_transitions.csv').open('w', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
            writer.writeheader()
            file.flush()
            os.fsync(file.fileno())

            # V2: the first edge uses the warm-up deadline. Each later edge
            # is scheduled relative to the *actual completion* of the
            # preceding sysfs LED write, never an overdue absolute deadline.
            # This prevents bursts of catch-up ON/OFF commands after stalls.
            previous_rel_ns = None
            previous_after_ns = None
            for trial, edge, rel_ns in edges:
                if previous_after_ns is None:
                    target_ns = first_edge_ns
                else:
                    interval_ns = rel_ns - previous_rel_ns
                    if interval_ns < round(args.min_delay * 1e9) - 1 or interval_ns > round(args.max_delay * 1e9) + 1:
                        raise RuntimeError('Invalid generated stimulus interval')
                    target_ns = previous_after_ns + interval_ns
                sleep_until(target_ns)
                before, after = sysfs_write(brightness_path,
                                             str(max_brightness if edge == 'ON' else 0))
                previous_rel_ns = rel_ns
                previous_after_ns = after
                row = {
                    'trial': trial,
                    'edge': edge,
                    'target_monotonic_ns': target_ns,
                    'before_write_monotonic_ns': before,
                    'after_write_monotonic_ns': after,
                    'write_duration_ns': after - before,
                    'schedule_lateness_ns': before - target_ns,
                    'utc_after_write': datetime.now(timezone.utc).isoformat(),
                }
                writer.writerow(row)
                file.flush()  # visible to readers; no per-edge fsync jitter
                completed += 1
                print(f'{trial:03d} {edge:3} lateness={row["schedule_lateness_ns"]/1e6:+.3f} ms '
                      f'write={row["write_duration_ns"]/1e6:.3f} ms', flush=True)
            # Persist complete CSV after the full stimulus sequence.
            os.fsync(file.fileno())
        # Preserve the last randomized OFF interval and the post-roll even
        # when earlier scheduled edges were delayed by the OS.
        final_off_interval_ns = total_ns - previous_rel_ns
        sleep_until(previous_after_ns + final_off_interval_ns + round(args.postroll * 1e9))
        manifest['status'] = 'completed'
        return 0
    except KeyboardInterrupt:
        manifest['status'] = 'interrupted'
        print('Interrupted: restoring LED state.', file=sys.stderr, flush=True)
        return 130
    except Exception as exc:
        manifest['status'] = 'error'
        manifest['error'] = repr(exc)
        raise
    finally:
        manifest['edges_logged'] = completed
        if armed:
            # Set LED off first, then restore the previous trigger. Trigger may
            # immediately resume its normal heartbeat behavior.
            try:
                sysfs_write(brightness_path, '0')
            except OSError as exc:
                print(f'WARNING: cannot turn LED off: {exc}', file=sys.stderr)
                manifest['restore_brightness_error'] = str(exc)
            try:
                sysfs_write(trigger_path, old_trigger)
                if old_trigger == 'none':
                    sysfs_write(brightness_path, str(old_brightness))
            except OSError as exc:
                print(f'WARNING: cannot restore LED trigger: {exc}', file=sys.stderr)
                manifest['restore_trigger_error'] = str(exc)
        manifest['ended_utc'] = datetime.now(timezone.utc).isoformat()
        save_manifest()
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == '__main__':
    sys.exit(main())
