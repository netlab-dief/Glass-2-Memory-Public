#!/usr/bin/env python3
"""EXPLORATORY KV260 GenX320 G2M analysis allowing ONLY zero-byte error buffers.

Uses host CLOCK_MONOTONIC for LED writes and DMA-completion timestamps.
Selects an LED ROI using *calibration trials only*, excluded from reported
latency statistics. This estimates software-referenced delivery latency,
NOT intrinsic photon-to-sensor latency.

Requires numpy. Read-only: never interacts with devices or LEDs.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import statistics
import sys

import numpy as np

W = H = 320
TILE = 16
GRID = W // TILE
NS = 1_000_000_000


def load_csv(path):
    with path.open(newline='') as f:
        return list(csv.DictReader(f))


def word_arrays(raw):
    words = np.frombuffer(raw, dtype='<u8')
    types = ((words >> np.uint64(60)) & np.uint64(15)).astype(np.uint8)
    valid = types <= 1
    if not np.any(valid):
        return (np.empty(0, np.uint64), np.empty(0, np.uint64),
                np.empty(0, np.uint64), np.empty(0, np.uint8))
    words = words[valid]
    types = types[valid]
    xb = ((words >> np.uint64(43)) & np.uint64(0x7ff)).astype(np.uint64)
    y = ((words >> np.uint64(32)) & np.uint64(0x7ff)).astype(np.uint64)
    mask = (words & np.uint64(0xffffffff)).astype(np.uint64)
    valid_xy = (y < H) & (xb < W) & (mask != 0)
    return xb[valid_xy], y[valid_xy], mask[valid_xy], types[valid_xy]


def count_roi(raw, roi):
    """Count actual expanded ON/OFF pixels in ROI without expanding whole frame."""
    x0, y0, x1, y1 = roi  # inclusive lower, exclusive upper
    xb, y, masks, types = word_arrays(raw)
    valid = (y >= y0) & (y < y1) & (xb < x1) & ((xb + 31) >= x0)
    if not np.any(valid):
        return (0, 0)
    xb, masks, types = xb[valid], masks[valid], types[valid]
    left = np.maximum(0, np.int64(x0) - xb.astype(np.int64)).astype(np.uint64)
    right = np.minimum(31, np.int64(x1 - 1) - xb.astype(np.int64)).astype(np.int64)
    widths = (right - left.astype(np.int64) + 1).astype(np.uint64)
    keep = (np.left_shift(np.uint64(1), widths) - np.uint64(1))
    clipped = (np.right_shift(masks, left) & keep).astype('<u4')
    bits = np.unpackbits(clipped.view(np.uint8), bitorder='little').reshape(-1, 32)
    counts = np.sum(bits, axis=1, dtype=np.int64)
    return (int(np.sum(counts[types == 1])), int(np.sum(counts[types == 0])))


def score_tiles(raw, expected_type):
    """+1 for correct event polarity, -1 for opposite, summed per 16x16 tile."""
    xb, y, masks, types = word_arrays(raw)
    if not len(xb):
        return np.zeros(GRID * GRID, dtype=np.int64)
    data = np.ascontiguousarray(masks.astype('<u4'))
    hits = np.unpackbits(data.view(np.uint8), bitorder='little').reshape(-1, 32)
    row, bit = np.nonzero(hits)
    if not len(row):
        return np.zeros(GRID * GRID, dtype=np.int64)
    x = xb[row] + bit.astype(np.uint64)
    yy = y[row]
    valid = x < W
    x, yy, rows = x[valid], yy[valid], row[valid]
    tile_index = (yy // TILE) * GRID + (x // TILE)
    weight = np.where(types[rows] == expected_type, 1, -1)
    return np.bincount(tile_index.astype(np.intp), weights=weight,
                       minlength=GRID * GRID).astype(np.int64)


def percentile(vals, p):
    return float(np.percentile(np.asarray(vals, dtype=np.float64), p)) if vals else None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('run', type=Path, help='Folder containing camera/ and led/')
    ap.add_argument('--roi', type=int, nargs=4, metavar=('X0', 'Y0', 'X1', 'Y1'),
                    help='Manually specified ROI (X1,Y1 exclusive); bypass auto calibration')
    ap.add_argument('--calibration-cycles', type=int, default=3,
                    help='Initial cycles used for auto ROI, excluded from latency stats (default 3)')
    ap.add_argument('--window-ms', type=int, default=550,
                    help='Maximum post-edge detection interval, default 550 ms')
    ap.add_argument('--min-events', type=int, default=12,
                    help='Minimum expected-polarity ROI events in a buffer, default 12')
    ap.add_argument('--out', type=Path, default=None,
                    help='Output directory (default RUN/analysis-errorsafe). Refuses overwriting')
    ap.add_argument('--allow-empty-error-buffers', action='store_true',
                    help='Exploratory only: allow only error-flagged buffers with exactly 0 payload bytes; exclude affected LED trials')
    args = ap.parse_args()
    if args.calibration_cycles < 1 or args.window_ms < 1 or args.min_events < 1:
        ap.error('Invalid thresholds or calibration cycle count')
    root = args.run
    out = args.out or root / 'analysis-errorsafe'
    if out.exists():
        ap.error(f'Output exists; refusing to overwrite: {out}')
    capdir, leddir = root / 'camera', root / 'led'
    manifest = json.loads((capdir / 'capture_manifest.json').read_text())
    ledman = json.loads((leddir / 'led_manifest.json').read_text())
    buffers = load_csv(capdir / 'v4l2_buffers.csv')
    edges = load_csv(leddir / 'led_transitions.csv')

    if not buffers or int(manifest['raw_bytes']) == 0:
        raise SystemExit('INVALID ACQUISITION: zero captured buffers; do not report G2M')
    if not edges or ledman.get('status') != 'completed':
        raise SystemExit('INVALID STIMULUS: missing transitions or unfinished LED run')
    if not manifest.get('all_v4l2_timestamps_monotonic'):
        raise SystemExit('INVALID CLOCK: V4L2 timestamps are not marked MONOTONIC')
    if manifest.get('stop_reason') in ('max_mib', 'low_disk_space'):
        raise SystemExit('INVALID ACQUISITION: capture reached file limit or disk reserve')
    if not all(x['edge'] in ('ON', 'OFF') for x in edges):
        raise SystemExit('Unknown LED edge label')
    errored = [b for b in buffers if int(b['buffer_error'])]
    if errored and not args.allow_empty_error_buffers:
        raise SystemExit('INVALID ACQUISITION: errored V4L2 buffers; '
                         'use --allow-empty-error-buffers for exploratory exclusion')
    if any(int(b['bytesused']) != 0 for b in errored):
        raise SystemExit('INVALID ACQUISITION: nonempty errored buffers are unsupported')
    if errored and len(errored) / len(buffers) > 0.02:
        raise SystemExit('INVALID ACQUISITION: too many error-marked buffers (>2%)')
    print(f'EXPLORATORY: {len(errored)} zero-byte error buffers / {len(buffers)} total. '
          'Nearby LED transitions will be excluded from latency statistics.')
    if int(manifest['raw_bytes']) != (capdir / 'events.raw').stat().st_size:
        raise SystemExit('INVALID ACQUISITION: raw file size mismatch')
    if len(edges) != 2 * int(ledman['cycles']):
        raise SystemExit('INVALID STIMULUS: incomplete edge sequence')

    edge_time = [int(e['after_write_monotonic_ns']) for e in edges]
    if any(a >= b for a, b in zip(edge_time, edge_time[1:])):
        raise SystemExit('INVALID STIMULUS: LED timestamps nonincreasing')
    stream_start = int(manifest['after_streamon_monotonic_ns'])
    stream_end = int(manifest['before_streamoff_monotonic_ns'])
    if not (stream_start < int(edges[0]['before_write_monotonic_ns']) and
            stream_end > edge_time[-1] + 1_000_000_000):
        raise SystemExit('INVALID ACQUISITION: capture did not span all LED edges + 1s')
    if any(int(b['bytesused']) < 0 or int(b['bytesused']) > int(manifest['sizeimage']) for b in buffers):
        raise SystemExit('INVALID ACQUISITION: bad buffer sizes')
    if sum(int(b['bytesused']) for b in buffers) != int(manifest['raw_bytes']):
        raise SystemExit('INVALID ACQUISITION: bytesused totals do not match')
    expected_offset = 0
    for i, b in enumerate(buffers):
        if int(b['file_offset']) != expected_offset:
            raise SystemExit(f'INVALID ACQUISITION: file offset mismatch at row {i}')
        expected_offset += int(b['bytesused'])
    seq = [int(b['sequence']) for b in buffers]
    if any(y != x+1 for x, y in zip(seq, seq[1:])):
        raise SystemExit('INVALID ACQUISITION: dropped or nonconsecutive buffer sequences')
    ts = [int(b['v4l2_timestamp_ns']) for b in buffers]
    if any(y < x for x, y in zip(ts, ts[1:])):
        raise SystemExit('INVALID ACQUISITION: buffer timestamps go backward')
    error_times = sorted(int(b['v4l2_timestamp_ns']) for b in errored)
    def intersects_error(before, cutoff):
        # Reject trials if a known missing DMA payload overlaps either
        # the 500 ms baseline or the 550 ms post-command detection window.
        # These are exclusions, not missing detections.
        from bisect import bisect_left
        start = before - 500_000_000
        k = bisect_left(error_times, start)
        return k < len(error_times) and error_times[k] < cutoff

    if args.roi:
        roi = tuple(args.roi)
        if not (0 <= roi[0] < roi[2] <= W and 0 <= roi[1] < roi[3] <= H):
            ap.error('ROI must satisfy 0<=X0<X1<=320, 0<=Y0<Y1<=320')
        candidates = []
        calibration = 0
    else:
        calibration = args.calibration_cycles
        if int(ledman['cycles']) <= calibration:
            raise SystemExit('Need more LED cycles than calibration-cycles')
        score = np.zeros(GRID*GRID, dtype=np.int64)
        calibration_time = []
        rejected_calibration = 0
        for j, e in enumerate(edges):
            if int(e['trial']) <= calibration:
                before = int(e['before_write_monotonic_ns'])
                after = int(e['after_write_monotonic_ns'])
                next_before = (int(edges[j+1]['before_write_monotonic_ns'])
                               if j+1 < len(edges) else stream_end)
                cutoff = min(after + args.window_ms * 1_000_000, next_before)
                if intersects_error(before, cutoff):
                    rejected_calibration += 1
                    continue
                calibration_time.append((after, 1 if e['edge'] == 'ON' else 0))
        print(f'ROI calibration windows retained: {len(calibration_time)}; '
              f'excluded near empty errors: {rejected_calibration}')
        if len(calibration_time) < 2 or len({t for _, t in calibration_time}) < 2:
            raise SystemExit('INSUFFICIENT CLEAN CALIBRATION: need ON and OFF windows; '
                             'supply a physically verified --roi from independent calibration')
        with (capdir / 'events.raw').open('rb') as raw:
            for b, t in zip(buffers, ts):
                active = None
                for et, typ in calibration_time:
                    if et <= t <= et + args.window_ms * 1_000_000:
                        active = typ
                        break
                if active is None:
                    continue
                raw.seek(int(b['file_offset']))
                score += score_tiles(raw.read(int(b['bytesused'])), active)
        if max(score) <= 0:
            raise SystemExit('AUTO ROI FAILED: no positive ON/OFF-correlated tile. '
                             'Choose ROI manually with --roi X0 Y0 X1 Y1')
        ordered = np.argsort(score)[::-1][:6]
        candidates = [{'tile_x': int(idx % GRID), 'tile_y': int(idx // GRID),
                       'center_x': int((idx % GRID)*TILE + TILE//2),
                       'center_y': int((idx // GRID)*TILE + TILE//2),
                       'signed_polarity_score': int(score[idx])} for idx in ordered]
        winner = candidates[0]
        cx, cy = winner['center_x'], winner['center_y']
        x0 = min(max(0, cx - 16), W - 32)
        y0 = min(max(0, cy - 16), H - 32)
        roi = (x0, y0, x0 + 32, y0 + 32)
        print('Auto ROI candidates (from calibration cycles only):')
        for c in candidates:
            print(f"  pixel center=({c['center_x']},{c['center_y']}), "
                  f"score={c['signed_polarity_score']}")

    print('ROI:', roi, 'calibration cycles excluded:', calibration)
    print('Reading buffer payloads and detecting polarity-specific bursts...')

    on = np.zeros(len(buffers), dtype=np.int64)
    off = np.zeros(len(buffers), dtype=np.int64)
    with (capdir / 'events.raw').open('rb') as raw:
        for i, b in enumerate(buffers):
            raw.seek(int(b['file_offset']))
            payload = raw.read(int(b['bytesused']))
            if len(payload) != int(b['bytesused']):
                raise SystemExit(f'Truncated raw payload for buffer {i}')
            if len(payload) % 8:
                raise SystemExit(f'Unaligned EVT21 payload in buffer {i}')
            on[i], off[i] = count_roi(payload, roi)

    rows = []
    for j, edge in enumerate(edges):
        typ = 'ON' if edge['edge'] == 'ON' else 'OFF'
        before = int(edge['before_write_monotonic_ns'])
        after = int(edge['after_write_monotonic_ns'])
        next_edge = int(edges[j+1]['before_write_monotonic_ns']) if j+1 < len(edges) else stream_end
        cutoff = min(after + args.window_ms*1_000_000, next_edge)
        near_error = intersects_error(before, cutoff)
        counts = on if typ == 'ON' else off
        opposite = off if typ == 'ON' else on
        pre = [int(counts[i]) for i, t in enumerate(ts)
               if before - 500_000_000 <= t < before]
        # Noise-based threshold is estimated only from buffers before this LED transition.
        baseline = percentile(pre, 95) or 0
        threshold = max(args.min_events, int(np.ceil(3*baseline + 4)))
        index = None
        for i, t in enumerate(ts):
            if t < after:
                continue
            if t >= cutoff:
                break
            if counts[i] >= threshold and counts[i] >= 2*opposite[i] + 4:
                index = i
                break
        row = {
            'trial': int(edge['trial']),
            'edge': typ,
            'phase': 'calibration' if int(edge['trial']) <= calibration else 'measurement',
            'quality': 'excluded_near_empty_error' if near_error else 'usable',
            'command_before_monotonic_ns': before,
            'command_after_monotonic_ns': after,
            'detection_threshold_events': threshold,
            'baseline_p95_events_per_buffer': round(baseline, 1),
            'detected': 1 if index is not None else 0,
            'buffer_sequence': seq[index] if index is not None else '',
            'buffer_v4l2_monotonic_ns': ts[index] if index is not None else '',
            'buffer_on_count': int(on[index]) if index is not None else '',
            'buffer_off_count': int(off[index]) if index is not None else '',
            'latency_from_after_write_ms': round((ts[index]-after)/1e6, 3) if index is not None else '',
            'latency_from_before_write_ms': round((ts[index]-before)/1e6, 3) if index is not None else '',
        }
        rows.append(row)

    measurement = [r for r in rows if r['phase'] == 'measurement']
    valid_measurement = [r for r in measurement if r['quality'] == 'usable']
    excluded_measurement = [r for r in measurement if r['quality'] != 'usable']
    result = {
        'definition': 'Earliest DMA-completion timestamp of a V4L2 buffer with '
                      'polarity-specific EVT21 activity in LED ROI after command',
        'NOT_intrinsic_sensor_latency': True,
        'limitations': 'Exploratory pilot only; zero-byte errors may hide missing event data. P95/P99 estimates from few trials are statistically unstable.',
        'clock': 'CLOCK_MONOTONIC for LED commands and kernel buffer timestamps',
        'command_reference': 'after sysfs brightness write syscall (not physical LED onset)',
        'roi_xyxy_exclusive': list(roi),
        'roi_source': 'manual' if args.roi else 'auto_calibration_cycles',
        'calibration_cycles_excluded': calibration,
        'min_events': args.min_events,
        'window_ms': args.window_ms,
        'exploratory_due_to_empty_error_buffers': bool(errored),
        'n_zero_byte_error_buffers': len(errored),
        'error_buffer_rows': [int(b['row']) for b in errored],
        'error_exclusion_rule': 'Exclude edge if zero-byte error completion '
                                'occurs between command_before - 500 ms and '
                                'min(command_after + window_ms, next command_before)',
        'n_measurement_edges': len(measurement),
        'n_excluded_due_to_error': len(excluded_measurement),
        'n_usable_edges': len(valid_measurement),
        'n_detected': sum(r['detected'] for r in valid_measurement),
        'n_missing': sum(1-r['detected'] for r in valid_measurement),
        'kernel_module_context': 'not automatically verified, record srcversion in lab notes',
        'roi_candidates': candidates,
    }
    for category in ('ON', 'OFF', 'ALL'):
        vals = [float(r['latency_from_after_write_ms']) for r in valid_measurement
                if r['detected'] and (r['edge'] == category or category == 'ALL')]
        name = category.lower()
        result[name] = {
            'detected': len(vals),
            'median_ms': percentile(vals, 50),
            'p95_ms': percentile(vals, 95),
            'p99_ms': percentile(vals, 99),
            'min_ms': min(vals) if vals else None,
            'max_ms': max(vals) if vals else None,
            'mean_ms': statistics.mean(vals) if vals else None,
        }

    out.mkdir(parents=True, exist_ok=False)
    with (out / 'detected_transitions.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    (out / 'results.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    for label in ('on','off','all'):
        s = result[label]
        print(f'{label.upper()}: n={s["detected"]}, median={s["median_ms"]} ms, '
              f'P95={s["p95_ms"]} ms, P99={s["p99_ms"]} ms')
    print(f'Detected {result["n_detected"]}/{result["n_usable_edges"]} '
          f'usable measurement transitions; missing={result["n_missing"]}; '
          f'excluded_near_error={result["n_excluded_due_to_error"]}')
    print(f'Saved {out / "results.json"} and detected_transitions.csv')
    print('CAUTION: Exploratory results. Zero-byte error buffers may represent lost events; '
          'do NOT present these numbers as a clean, publication-grade acquisition.')
    print('CAUTION: This is command-to-V4L2-buffer delivery, not optical onset or intrinsic sensor latency.')
    if result['n_missing']:
        print('WARNING: Undetected transitions; do not report aggregate percentiles without the miss rate.')


if __name__ == '__main__':
    main()
