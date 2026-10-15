#!/usr/bin/env python3
"""Read-only QA of the recorded KV260 heartbeat stimulus (v2)."""
import argparse
import csv
import json
from pathlib import Path
from statistics import median

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('led_dir', type=Path, help='Folder containing led_transitions.csv and led_manifest.json')
a = p.parse_args()
m = json.loads((a.led_dir / 'led_manifest.json').read_text())
with (a.led_dir / 'led_transitions.csv').open(newline='') as f:
    rows = list(csv.DictReader(f))
expected = int(m['cycles']) * 2
if m.get('status') != 'completed':
    raise SystemExit(f'FAIL: stimulus not completed: {m.get("status")}')
if len(rows) != expected:
    raise SystemExit(f'FAIL: {len(rows)} rows, expected {expected}')
intervals_ms = []
late_ms = []
previous = None
short_rows = []
long_rows = []
for i, row in enumerate(rows):
    if int(row['trial']) != i//2 + 1 or row['edge'] != ('ON' if i % 2 == 0 else 'OFF'):
        raise SystemExit(f'FAIL: wrong trial/order at row {i+1}')
    before = int(row['before_write_monotonic_ns'])
    after = int(row['after_write_monotonic_ns'])
    target = int(row['target_monotonic_ns'])
    if after < before:
        raise SystemExit(f'FAIL: inverted write timestamps at row {i+1}')
    if abs(int(row['schedule_lateness_ns']) - (before-target)) > 1:
        raise SystemExit(f'FAIL: incorrect lateness at row {i+1}')
    late_ms.append((before-target)/1e6)
    if previous is not None:
        gap_ms = (after-previous)/1e6
        intervals_ms.append(gap_ms)
        if gap_ms < float(m['min_delay_s'])*1000 - 5:
            short_rows.append((i+1, row['trial'], row['edge'], gap_ms))
        if gap_ms > float(m['max_delay_s'])*1000 + 100:
            long_rows.append((i+1, row['trial'], row['edge'], gap_ms))
    previous = after
print(f'Stimulus: {len(rows)} edges, protocol={m.get("protocol")}, status=completed')
print(f'Configured gap: {m["min_delay_s"]}–{m["max_delay_s"]} s')
if intervals_ms:
    print(f'Actual gap: min={min(intervals_ms):.3f} ms, median={median(intervals_ms):.3f} ms, max={max(intervals_ms):.3f} ms')
print(f'Max schedule lateness: {max(late_ms):.3f} ms')
print(f'Short gaps (< minimum - 5 ms): {len(short_rows)}')
print(f'Long gaps (> maximum + 100 ms): {len(long_rows)}')
for what, samples in [('short', short_rows), ('long', long_rows)]:
    for i, trial, edge, gap in samples[:10]:
        print(f'  {what}: row={i}, trial={trial}, edge={edge}, gap={gap:.3f} ms')
if short_rows:
    raise SystemExit('FAIL: commands too close together; acquisition stimulus not valid')
if long_rows:
    print('WARNING: scheduling stalls caused long intervals; retained and reported, not hidden')
if m.get('schedule_mode') != 'relative_to_previous_actual_write_completion':
    print('WARNING: not a v2 relative-to-actual-completion stimulus')
else:
    print('PASS: no compressed catch-up LED commands')
