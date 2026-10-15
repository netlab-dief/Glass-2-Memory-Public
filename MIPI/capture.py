import os
import time
from datetime import datetime
from picamera2 import Picamera2

out_dir = "/dev/shm/timestamped_frames"
os.makedirs(out_dir, exist_ok=True)

camera = Picamera2()

config = camera.create_still_configuration(
    main={"size": (1280, 720)},
    controls={"FrameDurationLimits": (33333, 33333)}  # ~30 FPS timing
)
camera.configure(config)

camera.start()

print(f"Capturing frames to {out_dir}... Press Ctrl+C to stop.")

try:
    while True:
        ts_str = datetime.now()
        file_path = os.path.join(out_dir, f"frame_{ts_str}.jpg")
        camera.capture_file(file_path)

except KeyboardInterrupt:
    print("\nStopping capture...")
finally:
    camera.stop()
    print("Camera stopped successfully.")
