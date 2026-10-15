# Glass2Memory - IP Camera setup

This script (`extract_frames.py`) reads a PCAPNG network capture containing an RTP stream of H.264 video, reconstructs the video frames, and decodes them into individual JPEG images. The extracted images are named using the exact timestamps from when the packets were captured.

## Start a new test 
To start a new test, open two terminals and run

```bash
ffplay -fflags nobuffer -flags low_delay -rtsp_transport udp -i "$camera_rtsp_url"
```
in the first terminal to start the transmission, and
```bash
tshark -i eth0 -w path-to-results/test.pcap
```
in the second terminal, to save the capture in a PCAP file.

## PCAPNG H.264 Frame Extractor

The script requires a Python virtual environment and the following dependencies:
- **`dpkt`**: For parsing the PCAPNG file and network protocols (Ethernet, IP, UDP).
- **`av`** (PyAV): FFmpeg bindings for Python, used to decode the H.264 byte stream into raw video frames.
- **`Pillow`**: For saving the raw frames as standard `.jpg` images.

You can install them via:
```bash
pip install dpkt av Pillow
```

---

## Code Breakdown

Here is a step-by-step explanation of every passage in the script:

### 1. Initialization and Setup
```python
pcap_file = "test_280426/transmission.pcap"
out_dir = "frames_out"
os.makedirs(out_dir, exist_ok=True)
```
The script defines the input `.pcap` file and the output directory (`frames_out`). `os.makedirs` ensures the output folder exists without throwing an error if it's already there.

### 2. Network Layer Parsing (Ethernet -> IP -> UDP)
```python
eth = dpkt.ethernet.Ethernet(buf)
if eth.type != dpkt.ethernet.ETH_TYPE_IP: continue
ip = eth.data
if ip.p != dpkt.ip.IP_PROTO_UDP: continue
udp = ip.data
```
The script uses `dpkt` to unpack the binary buffer (`buf`) layer by layer. It strictly filters for:
1. Ethernet frames containing IPv4 packets.
2. IPv4 packets containing UDP datagrams.

### 3. Dynamic RTP Detection & NAL Unit Extraction
```python
rtp = udp.data
if len(rtp) <= 12: continue

# Check if it looks like an RTP packet (Version 2)
if (rtp[0] >> 6) != 2: continue

# Commonly, dynamic payload type 96 is used for H.264
payload_type = rtp[1] & 0x7F
if payload_type != 96: continue

payload = rtp[12:]
if not payload: continue

nal_header = payload[0]
nal_type = nal_header & 0x1F
```
Instead of hardcoding specific UDP ports, the script dynamically detects RTP video packets from any UDP conversation. It checks if the packet has an **RTP Version 2** header (`rtp[0] >> 6 == 2`) and if its payload type corresponds to `96` (the dynamic payload type standardly assigned to H.264 streams). 

Because standard RTP headers are 12 bytes long, the script cuts off the first 12 bytes to isolate the bare H.264 payload. 
In H.264, the first byte of this payload dictates the **NAL Unit (Network Abstraction Layer) Type**. The script performs a bitwise AND operation (`& 0x1F`) to isolate the last 5 bits representing this precise type.

### 4. Handling Single and Fragmented NAL Units (FU-A)
H.264 frames are often too large to fit in a single network packet (MTU ~1500 bytes), so they are fragmented using a method called FU-A (Fragmentation Unit A).

**Single NAL Units (Types 1-23):**
```python
if nal_type >= 1 and nal_type <= 23:
    nal_units.append((ts, b'\x00\x00\x00\x01' + payload))
```
If the entire frame fits in one packet, it's saved instantly. The script prepends the H.264 start code identifier (`0x00 0x00 0x00 0x01`) which tells the video decoder that a new frame/block is starting.

**Fragmented NAL Units (Type 28 - FU-A):**
```python
elif nal_type == 28:
    fu_header = payload[1]
    s = (fu_header & 0x80) >> 7
    e = (fu_header & 0x40) >> 6
    orig_nal_type = fu_header & 0x1F
```
For fragmented packets, the script examines the second byte (`fu_header`). 
- `s = 1` means this is the **Start** of a fragmented frame.
- `e = 1` means this is the **End** of a fragmented frame.

If it's a **Start** packet, the script reconstructs the original NAL header and starts a temporary byte buffer. As subsequent continuation packets arrive, their payloads are appended. Once the **End** packet is reached, the fully reassembled NAL unit is bundled with the original timestamp (`current_ts`) and pushed to the list.

### 5. Video Decoding via PyAV
```python
codec = av.CodecContext.create('h264', 'r')
...
packets = codec.parse(nal_data)
for packet in packets:
    frames = codec.decode(packet)
```
With the H.264 stream reassembled, the script spins up an embedded FFmpeg H.264 decoder (`av.CodecContext`). The `parse()` method splits the NAL units logically, and `decode()` does the heavy lifting of converting compressed pixels into uncompressed visual frames.

### 6. Frame Formatting and Saving
```python
img = frame.to_image()
params = str(ts).replace('.', '_')
img_path = os.path.join(out_dir, f"frame_{params}.jpg")
img.save(img_path)
```
Finally, using Pillow (`frame.to_image()`), the uncompressed video matrix is converted into an Image object. 
The script replaces the decimal point in the Epoch timestamp with an underscore to act as a safe file name, and persists it to disk as a JPEG.

### 7. Flushing the Decoder
```python
for packet in codec.parse():
    for frame in codec.decode(packet):
        ...
```
Because video decoders hold on to several frames in memory/buffer for reference (B-frames, P-frames), a final empty `parse()` and `decode()` is triggered at the end of the script to flush out any lingering frames trapped inside the decoder's buffer.