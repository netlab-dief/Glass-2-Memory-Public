import sys
import dpkt
import os
import av
import argparse
import datetime

def main(input_pcap):
    pcap_file = input_pcap
    out_dir = "frames_out"
    os.makedirs(out_dir, exist_ok=True)
    
    # 1. Parse PCAP and extract H.264 NAL units
    nal_units = []
    
    with open(pcap_file, "rb") as f:
        p = dpkt.pcapng.Reader(f)
        
        current_nal = bytearray()
        current_ts = None
        
        for ts, buf in p:
            try:
                eth = dpkt.ethernet.Ethernet(buf)
                if eth.type != dpkt.ethernet.ETH_TYPE_IP: continue
                ip = eth.data
                if ip.p != dpkt.ip.IP_PROTO_UDP: continue
                udp = ip.data
                
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
                
                if nal_type >= 1 and nal_type <= 23:
                    # Single NAL unit (Coded slice, SPS, PPS, SEI, etc.)
                    nal_units.append((ts, b'\x00\x00\x00\x01' + payload))
                        
                elif nal_type == 28:
                    # FU-A
                    fu_header = payload[1]
                    s = (fu_header & 0x80) >> 7
                    e = (fu_header & 0x40) >> 6
                    orig_nal_type = fu_header & 0x1F
                    
                    if s == 1:
                        # Start of fragmented NAL
                        reconstructed_header = bytes([(nal_header & 0xE0) | orig_nal_type])
                        current_nal = bytearray(b'\x00\x00\x00\x01' + reconstructed_header + payload[2:])
                        current_ts = ts
                    else:
                        # Continuation or End
                        current_nal.extend(payload[2:])
                        if e == 1:
                            nal_units.append((current_ts, bytes(current_nal)))
                            current_nal = bytearray()
            except Exception as e:
                pass

    print(f"Extracted {len(nal_units)} NAL units.")

    # 2. Decode using PyAV
    codec = av.CodecContext.create('h264', 'r')
    frame_count = 0
    

    for ts, nal_data in nal_units:
        try:
            # Parse the NAL data into packets
            packets = codec.parse(nal_data)
            for packet in packets:
                frames = codec.decode(packet)
                for frame in frames:
                    # Save the frame as image
                    img = frame.to_image()
                    # Format timestamp as YYYY-MM-DD_HH-MM-SS.mmmmmm to match the log
                    ts_dt = datetime.datetime.fromtimestamp(ts)
                    params = ts_dt.strftime('%Y-%m-%d_%H-%M-%S.%f')
                    img_path = os.path.join(out_dir, f"frame_{params}.jpg")
                    img.save(img_path)
                    frame_count += 1
                    if frame_count % 100 == 0:
                        print(f"Saved {frame_count} frames...")
        except av.error.InvalidDataError:
            # Ignore invalid data errors, normal when starting decoding mid-stream
            pass
        except Exception as e:
            print(f"Decode error: {e}")
            pass
            
    # Flush the decoder
    try:
        for packet in codec.parse():
            for frame in codec.decode(packet):
                img = frame.to_image()
                img_path = os.path.join(out_dir, f"frame_flush_{frame_count}.jpg")
                img.save(img_path)
                frame_count += 1
    except Exception:
        pass
        
    print(f"Done! Saved {frame_count} images to {out_dir}/")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract frames from H.264 video in a PCAP file")
    parser.add_argument("input_pcap", help="Path to the input PCAP file")
    args = parser.parse_args()
    main(args.input_pcap)