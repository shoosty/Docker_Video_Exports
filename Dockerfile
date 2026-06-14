# Docker_Video_Exports — RunPod serverless container that renders
# a song's audio + slide deck into an MP4 video.
#
# Same architecture as Docker_Ace_Step but no model weights — just
# Python + ffmpeg + the upload glue. Image ends up around 500 MB.
FROM python:3.12-slim

# ffmpeg is the workhorse. apt's bundled version (6.x) has xfade +
# drawtext + concat demuxer, which is all we need for v1.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY handler.py .

# RunPod serverless invokes handler.handler(job) via
# runpod.serverless.start(...). The CMD just keeps the process up.
CMD ["python", "-u", "handler.py"]
