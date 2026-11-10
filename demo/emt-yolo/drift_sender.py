#!/usr/bin/env python3
"""EMT dataset sender (the vehicle dashcam), sized for the 5G uplink.

Wraps the colleague's UDPStreamingServer (udp_streaming_server.py, unchanged)
and re-encodes each frame before sending. The dataset frames are 2560x1440
JPEGs of ~0.96 MB, i.e. ~92 Mbps at 12 FPS, while the best uplink measured on
the testbed is ~55 Mbps (18 Mbps under a QOS_L session). At 1280x720 a frame
is ~0.15 MB (~14 Mbps at 12 FPS). The model sees 640 px either way.
--resize 0 sends the original files untouched.
"""
import argparse
import io
import os
import time
from pathlib import Path

from PIL import Image

from udp_protocol import encode_frame
from udp_streaming_server import UDPStreamingServer


class DriftSender(UDPStreamingServer):
    def __init__(self, *a, width=1280, quality=85, pace=0.0, dup=1, **kw):
        super().__init__(*a, **kw)
        self.width, self.quality, self.pace, self.dup = width, quality, pace, max(1, dup)
        self.bytes_sent = 0

    def send_frame(self, image_path, frame_id):
        data = Path(image_path).read_bytes()
        if self.width:
            with Image.open(io.BytesIO(data)) as im:
                if im.width > self.width:
                    # decode at reduced scale straight away (JPEG DCT scaling):
                    # 720p -> 640 px live costs a few ms instead of a full decode
                    im.draft('RGB', (self.width, round(im.height * self.width / im.width)))
                    im = im.convert('RGB').resize(
                        (self.width, round(im.height * self.width / im.width)), Image.BILINEAR)
                    buf = io.BytesIO()
                    im.save(buf, 'JPEG', quality=self.quality)
                    data = buf.getvalue()
        self.bytes_sent += len(data)
        packets = list(encode_frame(data, self.session, frame_id))
        # --dup 2 sends every datagram twice, the copies after all originals so
        # they are ~40 ms apart. The receiver keeps the first copy of each chunk.
        # Measured 2026-10-31: a QOS_L flow loses ~5% of datagrams, so 69% of
        # 150 kB frames arrive whole; 640 px frames sent twice (~7 Mbit/s,
        # under the 15 Mbit/s GBR) arrived 100% whole.
        packets = packets * self.dup
        # A frame sent as one burst of ~95 packets overflows the 5G uplink
        # buffer and every frame loses a packet (none complete). --pace spreads
        # the packets over that fraction of the frame interval instead.
        gap = self.pace / self.fps / len(packets) if self.pace else 0
        start = time.monotonic()
        for i, packet in enumerate(packets):
            self.socket.sendto(packet, self.target)
            if gap:
                time.sleep(max(0, start + (i + 1) * gap - time.monotonic()))
        if frame_id % 100 == 99:
            print(f'Average frame {self.bytes_sent / (frame_id + 1) / 1e3:.0f} kB', flush=True)


    def stream_directory(self, image_dir, loop=False):
        """As the parent's, but follows image_dir when it is a link that gets
        re-pointed (drift-dashcam-scene day|night): the frame list is reloaded
        at the next frame, so a scene switch needs no restart."""
        def frames(where):
            return sorted(p for p in Path(where).rglob('*')
                          if p.suffix.lower() in {'.jpg', '.jpeg', '.png'} and p.is_file())
        scene = os.path.realpath(image_dir)
        images = frames(scene)
        if not images:
            raise ValueError(f'No images found in {image_dir}')
        print(f'Found {len(images)} images in {scene}; replay={loop}', flush=True)
        while True:
            for path in images:
                now_scene = os.path.realpath(image_dir)
                if now_scene != scene:
                    new = frames(now_scene)
                    if new:
                        scene, images = now_scene, new
                        print(f'Scene -> {scene} ({len(images)} images)', flush=True)
                        break
                started = time.monotonic()
                self.send_frame(path, self.frame_index)
                self.frame_index += 1
                if self.frame_index % 100 == 0:
                    print(f'Sent {self.frame_index} frames', flush=True)
                time.sleep(max(0, 1/self.fps - (time.monotonic()-started)))
            else:
                if not loop:
                    return


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image-dir', default='/data/images/test/images')
    p.add_argument('--target-host', default=os.getenv('TARGET_HOST', 'localhost'))
    p.add_argument('--target-port', type=int, default=5005)
    p.add_argument('--loop', action='store_true')
    p.add_argument('--fps', type=float, default=12)
    p.add_argument('--resize', type=int, default=int(os.getenv('RESIZE_WIDTH', '1280')),
                   help='Frame width to send; 0 keeps the original files')
    p.add_argument('--quality', type=int, default=int(os.getenv('QUALITY', '85')))
    p.add_argument('--dup', type=int, default=int(os.getenv('DUP', '1')),
                   help='Send every datagram this many times (loss resilience)')
    p.add_argument('--pace', type=float, default=float(os.getenv('PACE', '0')),
                   help='Spread each frame over this fraction of the frame interval (0 = burst)')
    a = p.parse_args()
    s = DriftSender(a.target_host, a.target_port, a.fps, width=a.resize, quality=a.quality, pace=a.pace,
                    dup=a.dup)
    try:
        s.start()
        s.stream_directory(a.image_dir, a.loop)
    except KeyboardInterrupt:
        pass
    finally:
        s.stop()
