FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependency layer first so application edits do not invalidate the pip cache.
COPY pyproject.toml ./
RUN pip install --upgrade pip setuptools wheel \
 && python - <<'PY'
import tomllib, subprocess, sys
cfg = tomllib.load(open("pyproject.toml", "rb"))["project"]
pkgs = cfg["dependencies"] + cfg["optional-dependencies"]["dev"]
subprocess.check_call([sys.executable, "-m", "pip", "install", *pkgs])
PY

COPY . .

# Spec 21: never run application code as root.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Spec 23.4: container healthcheck uses the readiness probe.
HEALTHCHECK --interval=10s --timeout=3s --start-period=40s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/v1/health/ready',timeout=2).status==200 else 1)"

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
