# Use the official Python 3.11-slim image for a small footprint, on Debian BOOKWORM — the same Debian
# release as Raspberry Pi OS on the appliance (the floating `3.11-slim` tag moved to trixie upstream; 1.0
# deliberately stays on bookworm). Pinned by multi-arch index digest so the resolved patch level is
# checkable from the repo: python:3.11.16-slim-bookworm as of 2026-09-22
# (`docker buildx imagetools inspect python:3.11.16-slim-bookworm`), past the 3.11.13 floor that closes
# the tarfile-extraction-filter CVE cluster (CVE-2024-12718, CVE-2025-4138/4330/4517).
FROM python:3.11.16-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies if needed (e.g., for Pillow)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy only the requirements file first to leverage Docker's cache
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .
RUN chmod +x docker-entrypoint.sh

# Run as a non-root user (C1 hardening: contains any future write-primitive). uid/gid 1000 matches the
# host owner of the ./Artwork and ./data bind mounts on the Pi and the dev laptop, so the unprivileged
# container can still create/write library files, the SQLite DB, and the appliance request files.
RUN groupadd -g 1000 app && useradd -m -u 1000 -g 1000 app && chown -R app:app /app
USER app

# Expose the port the app runs on
EXPOSE 8000

# ENTRYPOINT migrates once (single process) then exec's the command below — see docker-entrypoint.sh
# (ADR-037). ENTRYPOINT (not CMD) so the appliance compose's `command:` override still gets migrated-first.
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
