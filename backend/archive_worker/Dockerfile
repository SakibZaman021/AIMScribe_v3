# AIMScribe Archive Worker
#
# Runs on the AIMS LAB server. Exposes no ports and runs as a non-root user: it
# holds every consultation recording, so it gets the smallest surface possible.

FROM python:3.12-slim

# tzdata is required. Without it the hospital's local date is unavailable and
# recordings would be filed by UTC date, scattering evening consultations across
# two folders.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# restore.py is not run by the worker, but it is the only way to read a
# cloud copy back, so it ships in the image that made them.
COPY worker.py archive.py catalogue.py cloudcopy.py restore.py ./

# Fail the build, not the night shift, if the FLAC codec is not usable.
RUN python -c "import soundfile; print(soundfile.__libsndfile_version__)"

# The archive volume is mounted here and must be writable by this user.
RUN useradd --create-home --uid 10001 aimscribe \
    && mkdir -p /archive \
    && chown -R aimscribe:aimscribe /app /archive
USER aimscribe

ENV PYTHONUNBUFFERED=1 \
    AIMS_ARCHIVE_ROOT=/archive

# No EXPOSE: this container accepts no inbound connections.

# Liveness only - the worker has no HTTP surface to probe.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import os,sys; sys.exit(0 if os.path.isdir(os.environ['AIMS_ARCHIVE_ROOT']) else 1)"

CMD ["python", "worker.py"]
