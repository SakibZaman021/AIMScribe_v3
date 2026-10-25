# The watchman. One dependency, because it runs beside the recordings and
# every dependency here is attack surface (SRS-SRV-08).
FROM python:3.12-slim

RUN pip install --no-cache-dir "asyncpg>=0.29" \
    && groupadd -r aims && useradd -r -g aims aims     && mkdir /state && chown aims:aims /state
# /state is a named volume; Docker gives a new one this folder's owner, so the
# monitor can keep what it has sent (no repeated alerts, the weekly report).

WORKDIR /app
COPY checks.py monitor.py ./

USER aims
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# It reads; it never writes to the database, and the role it uses could not
# write if it tried.
CMD ["python", "monitor.py"]
