# DRIFT demo video-server workload — a CRIU-migratable pod that streams a looping
# clip with an on-screen frame counter (so a migration freeze is visible in the
# player). The clip is GENERATED AT BUILD TIME with ffmpeg and baked in, so the
# RUNTIME is pure-stdlib python (CRIU-clean) and the migration target — a fresh
# copy of this image — already has the file.
FROM python:3-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core && \
    mkdir -p /media && \
    ffmpeg -f lavfi -i "testsrc2=size=640x360:rate=15:duration=30" \
      -vf "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='DRIFT frame %{n}':x=20:y=20:fontsize=30:fontcolor=white:box=1:boxcolor=black@0.6" \
      -c:v libx264 -pix_fmt yuv420p -movflags +faststart -an /media/loop.mp4 && \
    apt-get purge -y ffmpeg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY serve.py /serve.py
EXPOSE 9000
CMD ["python3", "/serve.py"]
