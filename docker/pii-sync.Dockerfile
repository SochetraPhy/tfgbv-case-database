# The reconciler runs from a baked image rather than a bind-mounted ./scripts.
# On macOS, Docker Desktop bind mounts under ~/Documents (TCC-protected) fail reads
# with errno 35 "Resource deadlock would occur" — and Python silently sees a 0-byte
# source file, so the container exits 0 having done nothing. Copying at build time
# avoids the host filesystem entirely at runtime.
#
# After editing either script:  docker compose build pii-access-sync
FROM python:3.12-alpine
WORKDIR /app
COPY scripts/seed_schema.py scripts/pii_access_sync.py /app/scripts/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python3", "scripts/pii_access_sync.py"]
