#!/usr/bin/env python3
"""Read-only consistency checks for a stage-2 GenX320 G2M capture."""
import argparse
import csv
import json
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("capture_dir", type=Path)
args = ap.parse_args()
folder = args.capture_dir
rows = list(csv.DictReader((folder / "v4l2_buffers.csv").open(newline="")))
manifest = json.loads((folder / "capture_manifest.json").read_text())
actual = (folder / "events.raw").stat().st_size

offset = 0
previous_sequence = None
previous_ts = None
errors = 0
for index, row in enumerate(rows):
    if int(row["row"]) != index:
        raise SystemExit(f"FAIL: row number mismatch at {index}")
    if int(row["file_offset"]) != offset:
        raise SystemExit(f"FAIL: file offset mismatch at {index}")
    size = int(row["bytesused"])
    if size < 0 or size > manifest["sizeimage"]:
        raise SystemExit(f"FAIL: invalid buffer size at {index}")
    offset += size
    seq = int(row["sequence"])
    if previous_sequence is not None and seq != previous_sequence + 1:
        raise SystemExit(f"FAIL: nonconsecutive sequence at {index}: {previous_sequence} -> {seq}")
    previous_sequence = seq
    ts = int(row["v4l2_timestamp_ns"])
    if previous_ts is not None and ts < previous_ts:
        raise SystemExit(f"FAIL: backward V4L2 timestamp at {index}")
    previous_ts = ts
    errors += int(row["buffer_error"])

if offset != actual or actual != int(manifest["raw_bytes"]):
    raise SystemExit(f"FAIL: raw bytes mismatch (CSV={offset}, disk={actual}, manifest={manifest['raw_bytes']})")
if len(rows) != int(manifest["v4l2_buffers"]):
    raise SystemExit("FAIL: buffer count mismatch")
print(f"PASS: {len(rows)} buffers, {actual:,} raw bytes, {errors} error-marked buffers")
print("V4L2 timestamp clock monotonic:", manifest["all_v4l2_timestamps_monotonic"])
print("V4L2 timestamp source EOF:", manifest["all_v4l2_timestamps_eof"])
print("Capture stop reason:", manifest["stop_reason"])
if errors:
    print("WARNING: buffer errors need investigation before G2M analysis")
if not manifest["all_v4l2_timestamps_monotonic"]:
    print("WARNING: do not directly compare V4L2 timestamps with LED CLOCK_MONOTONIC")
