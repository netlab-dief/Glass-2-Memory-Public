// KV260 / GenX320 G2M recorder -- stage 2
// Stores EVT21 bytes unchanged and one metadata row per V4L2 DQBUF.
// No camera configuration or GPIO operations are performed.
// Build: g++ -O2 -Wall -Wextra -std=c++17 -o g2m_record_v4l2 g2m_record_v4l2.cpp

#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/poll.h>
#include <sys/statvfs.h>
#include <fcntl.h>
#include <unistd.h>
#include <signal.h>
#include <time.h>

#include <algorithm>
#include <cerrno>
#include <climits>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fs = std::filesystem;
static volatile sig_atomic_t stopping = 0;

static void signal_handler(int) { stopping = 1; }

static uint64_t mono_ns() {
    timespec ts{};
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        throw std::runtime_error("clock_gettime failed");
    return uint64_t(ts.tv_sec) * 1000000000ULL + uint64_t(ts.tv_nsec);
}

static std::string err(const std::string& context) {
    return context + ": " + std::strerror(errno);
}

static void xioctl(int fd, unsigned long request, void* arg, const char* desc) {
    int rc;
    do { rc = ioctl(fd, request, arg); } while (rc == -1 && errno == EINTR && !stopping);
    if (rc == -1) throw std::runtime_error(err(desc));
}

struct Options {
    std::string device = "/dev/video0";
    std::string out;
    unsigned seconds = 8;
    unsigned buffers = 4;
    uint64_t max_mib = 64;
    uint64_t reserve_mib = 256;
};

static uint64_t number(const char* s, const char* label) {
    if (!*s || *s == '-') throw std::runtime_error(std::string("Invalid ") + label);
    char* end = nullptr;
    errno = 0;
    unsigned long long v = std::strtoull(s, &end, 10);
    if (errno || !end || *end) throw std::runtime_error(std::string("Invalid ") + label);
    return static_cast<uint64_t>(v);
}

static Options parse(int argc, char** argv) {
    Options o;
    for (int i=1; i<argc; ++i) {
        std::string a = argv[i];
        if (a == "--help") {
            std::cout <<
              "Usage: g2m_record_v4l2 --out-dir NEW_DIRECTORY [options]\n"
              "  --device PATH     V4L2 device (default /dev/video0)\n"
              "  --seconds N       Maximum recording duration (default 8)\n"
              "  --buffers N       MMAP buffers, 2..32 (default 4)\n"
              "  --max-mib N       Maximum raw size in MiB (default 64)\n"
              "  --reserve-mib N   Stop if free disk falls below N MiB (default 256)\n"
              "Outputs: events.raw, v4l2_buffers.csv, capture_manifest.json\n"
              "Requires preconfigured PSE2 EVT21 /dev/video0; no stream restarts.\n";
            std::exit(0);
        }
        if (++i >= argc) throw std::runtime_error("Missing argument to " + a);
        std::string val = argv[i];
        if (a == "--out-dir") o.out = val;
        else if (a == "--device") o.device = val;
        else if (a == "--seconds") o.seconds = static_cast<unsigned>(number(val.c_str(), "seconds"));
        else if (a == "--buffers") o.buffers = static_cast<unsigned>(number(val.c_str(), "buffers"));
        else if (a == "--max-mib") o.max_mib = number(val.c_str(), "max-mib");
        else if (a == "--reserve-mib") o.reserve_mib = number(val.c_str(), "reserve-mib");
        else throw std::runtime_error("Unknown option: " + a);
    }
    if (o.out.empty()) throw std::runtime_error("--out-dir NEW_DIRECTORY is required");
    if (!o.seconds || o.buffers < 2 || o.buffers > 32 || !o.max_mib)
        throw std::runtime_error("Need seconds>=1, 2<=buffers<=32 and max-mib>=1");
    if (o.max_mib > UINT64_MAX / 1048576ULL || o.reserve_mib > UINT64_MAX / 1048576ULL)
        throw std::runtime_error("MiB limit is too large");
    return o;
}

static void write_all(int fd, const void* data, size_t n) {
    const char* p = static_cast<const char*>(data);
    while (n) {
        ssize_t rc = write(fd, p, n);
        if (rc < 0 && errno == EINTR) continue;
        if (rc <= 0) throw std::runtime_error(err("write events.raw"));
        p += rc;
        n -= static_cast<size_t>(rc);
    }
}

static uint64_t free_bytes(const fs::path& path) {
    struct statvfs v{};
    if (statvfs(path.c_str(), &v) < 0) throw std::runtime_error(err("statvfs"));
    return uint64_t(v.f_bavail) * v.f_frsize;
}

struct Mapping { void* ptr = MAP_FAILED; size_t len = 0; };
struct Capture {
    int fd = -1;
    int raw = -1;
    std::vector<Mapping> maps;
    bool streaming = false;
    ~Capture() {
        if (fd >= 0 && streaming) {
            v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            if (ioctl(fd, VIDIOC_STREAMOFF, &type) < 0)
                std::cerr << "WARNING: STREAMOFF failed: " << std::strerror(errno) << "\n";
        }
        for (auto& m : maps) if (m.ptr != MAP_FAILED) munmap(m.ptr, m.len);
        if (raw >= 0) close(raw);
        if (fd >= 0) close(fd);
    }
};

int main(int argc, char** argv) {
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);
    try {
        Options o = parse(argc, argv);
        fs::path out(o.out);
        if (!fs::create_directory(out))
            throw std::runtime_error("Output directory already exists; refusing overwrite: " + o.out);

        Capture cap;
        std::ofstream csv(out / "v4l2_buffers.csv", std::ios::out | std::ios::trunc);
        if (!csv) throw std::runtime_error("Could not open metadata CSV");
        csv << "row,sequence,buffer_index,file_offset,bytesused,flags,buffer_error,"
               "v4l2_timestamp_ns,dq_before_monotonic_ns,dq_after_monotonic_ns\n";
        csv.flush();

        cap.raw = open((out / "events.raw").c_str(), O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0644);
        if (cap.raw < 0) throw std::runtime_error(err("open events.raw"));
        cap.fd = open(o.device.c_str(), O_RDWR | O_NONBLOCK | O_CLOEXEC);
        if (cap.fd < 0) throw std::runtime_error(err("open video device"));

        v4l2_capability caps{};
        xioctl(cap.fd, VIDIOC_QUERYCAP, &caps, "VIDIOC_QUERYCAP");
        const uint32_t features = (caps.capabilities & V4L2_CAP_DEVICE_CAPS)
                                   ? caps.device_caps : caps.capabilities;
        if (!(features & V4L2_CAP_VIDEO_CAPTURE) || !(features & V4L2_CAP_STREAMING))
            throw std::runtime_error("Device does not offer capture+streaming");

        v4l2_format fmt{};
        fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        xioctl(cap.fd, VIDIOC_G_FMT, &fmt, "VIDIOC_G_FMT");
        const auto& pix = fmt.fmt.pix;
        if (pix.pixelformat != v4l2_fourcc('P','S','E','2') ||
            pix.width != 320 || pix.height != 320)
            throw std::runtime_error("Expected configured PSE2 320x320; run manual setup before recorder");

        std::cout << "Format PSE2 " << pix.width << "x" << pix.height
                  << " sizeimage=" << pix.sizeimage << "\n";
        if (pix.sizeimage != 65536)
            throw std::runtime_error("Expected 64 KiB video module (sizeimage=65536); refusing test");

        if (free_bytes(out) <= o.reserve_mib * 1048576ULL)
            throw std::runtime_error("Available disk space is below reserve-mib");

        v4l2_requestbuffers req{};
        req.count = o.buffers;
        req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        req.memory = V4L2_MEMORY_MMAP;
        xioctl(cap.fd, VIDIOC_REQBUFS, &req, "VIDIOC_REQBUFS");
        if (req.count < 2) throw std::runtime_error("Driver allocated fewer than 2 buffers");

        cap.maps.resize(req.count);
        for (unsigned i=0; i<req.count; ++i) {
            v4l2_buffer buf{};
            buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            buf.memory = V4L2_MEMORY_MMAP;
            buf.index = i;
            xioctl(cap.fd, VIDIOC_QUERYBUF, &buf, "VIDIOC_QUERYBUF");
            cap.maps[i].len = buf.length;
            cap.maps[i].ptr = mmap(nullptr, buf.length, PROT_READ | PROT_WRITE,
                                   MAP_SHARED, cap.fd, buf.m.offset);
            if (cap.maps[i].ptr == MAP_FAILED) throw std::runtime_error(err("mmap"));
            xioctl(cap.fd, VIDIOC_QBUF, &buf, "VIDIOC_QBUF");
        }

        v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        const uint64_t before_streamon = mono_ns();
        xioctl(cap.fd, VIDIOC_STREAMON, &type, "VIDIOC_STREAMON");
        cap.streaming = true;
        const uint64_t after_streamon = mono_ns();
        std::cout << "STREAMING. Run LED stimulus only after this message.\n" << std::flush;

        const uint64_t limit = o.max_mib * 1048576ULL;
        const uint64_t deadline = after_streamon + uint64_t(o.seconds) * 1000000000ULL;
        uint64_t offset = 0, count = 0, last_space_check = after_streamon;
        uint64_t last_progress = after_streamon;
        std::string reason = "duration";
        bool v4l2_mono = true;
        bool v4l2_eof = true;

        while (!stopping) {
            const uint64_t now = mono_ns();
            if (now >= deadline) { reason = "duration"; break; }
            if (now - last_space_check >= 1000000000ULL) {
                last_space_check = now;
                if (free_bytes(out) <= o.reserve_mib * 1048576ULL) {
                    reason = "low_disk_space"; break;
                }
            }

            pollfd pfd{cap.fd, POLLIN, 0};
            const int ms = static_cast<int>(
                std::min<uint64_t>(500, (deadline - now + 999999ULL) / 1000000ULL));
            int p = poll(&pfd, 1, ms);
            if (p < 0) {
                if (errno == EINTR) continue;
                throw std::runtime_error(err("poll"));
            }
            if (p == 0) continue;
            if (pfd.revents & (POLLERR | POLLNVAL | POLLHUP))
                throw std::runtime_error("V4L2 poll signaled an error");

            v4l2_buffer buf{};
            buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            buf.memory = V4L2_MEMORY_MMAP;
            const uint64_t dq_before = mono_ns();
            int ret = ioctl(cap.fd, VIDIOC_DQBUF, &buf);
            const uint64_t dq_after = mono_ns();
            if (ret < 0 && (errno == EINTR || errno == EAGAIN)) continue;
            if (ret < 0) throw std::runtime_error(err("VIDIOC_DQBUF"));
            if (buf.index >= cap.maps.size() ||
                buf.bytesused > cap.maps[buf.index].len)
                throw std::runtime_error("Driver returned invalid buffer index/bytesused");

            if (offset + buf.bytesused > limit) {
                reason = "max_mib";
                break; // STREAMOFF will return the dequeued buffer
            }

            const uint64_t ts_ns = uint64_t(buf.timestamp.tv_sec) * 1000000000ULL
                                 + uint64_t(buf.timestamp.tv_usec) * 1000ULL;
            const bool is_error = (buf.flags & V4L2_BUF_FLAG_ERROR) != 0;
            v4l2_mono &= (buf.flags & V4L2_BUF_FLAG_TIMESTAMP_MASK) ==
                         V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC;
            v4l2_eof &= (buf.flags & V4L2_BUF_FLAG_TSTAMP_SRC_MASK) ==
                        V4L2_BUF_FLAG_TSTAMP_SRC_EOF;

            write_all(cap.raw, cap.maps[buf.index].ptr, buf.bytesused);
            csv << count << ',' << buf.sequence << ',' << buf.index << ','
                << offset << ',' << buf.bytesused << ',' << buf.flags << ','
                << (is_error ? 1 : 0) << ',' << ts_ns << ','
                << dq_before << ',' << dq_after << '\n';
            if (!csv) throw std::runtime_error("Could not write metadata CSV");
            csv.flush();
            if (!csv) throw std::runtime_error("Could not flush metadata CSV");
            offset += buf.bytesused;
            ++count;

            xioctl(cap.fd, VIDIOC_QBUF, &buf, "VIDIOC_QBUF");
            if (dq_after - last_progress > 5000000000ULL) {
                std::cout << "buffers=" << count << " bytes=" << offset
                          << " elapsed_s=" << (dq_after-after_streamon)/1e9 << '\n'
                          << std::flush;
                last_progress = dq_after;
            }
        }
        if (stopping) reason = "signal";
        const uint64_t before_streamoff = mono_ns();
        // Stop streaming in a controlled way, only once.
        if (cap.streaming) {
            if (ioctl(cap.fd, VIDIOC_STREAMOFF, &type) < 0)
                throw std::runtime_error(err("VIDIOC_STREAMOFF"));
            cap.streaming = false;
        }
        const uint64_t after_streamoff = mono_ns();
        if (fsync(cap.raw) != 0) throw std::runtime_error(err("fsync events.raw"));
        csv.close();

        std::ofstream manifest(out / "capture_manifest.json");
        if (!manifest) throw std::runtime_error("Could not write manifest");
        manifest << "{\n"
                 << "  \"device\": \"" << o.device << "\",\n"
                 << "  \"format\": \"PSE2 EVT21\",\n"
                 << "  \"width\": 320,\n"
                 << "  \"height\": 320,\n"
                 << "  \"sizeimage\": " << pix.sizeimage << ",\n"
                 << "  \"v4l2_buffers\": " << count << ",\n"
                 << "  \"raw_bytes\": " << offset << ",\n"
                 << "  \"before_streamon_monotonic_ns\": " << before_streamon << ",\n"
                 << "  \"after_streamon_monotonic_ns\": " << after_streamon << ",\n"
                 << "  \"before_streamoff_monotonic_ns\": " << before_streamoff << ",\n"
                 << "  \"after_streamoff_monotonic_ns\": " << after_streamoff << ",\n"
                 << "  \"all_v4l2_timestamps_monotonic\": " << (v4l2_mono ? "true":"false") << ",\n"
                 << "  \"all_v4l2_timestamps_eof\": " << (v4l2_eof ? "true":"false") << ",\n"
                 << "  \"stop_reason\": \"" << reason << "\"\n"
                 << "}\n";
        manifest.close();
        std::cout << "DONE. reason=" << reason << " buffers=" << count
                  << " bytes=" << offset << " output=" << out << "\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "ERROR: " << e.what() << "\n"
                  << "Do not retry STREAMON after a kernel DMA/sensor error. Inspect dmesg first.\n";
        return 1;
    }
}
