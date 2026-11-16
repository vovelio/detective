FROM python:3.13.18-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY infra/requirements-runtime.lock /app/requirements-runtime.lock
RUN python -m pip install --no-cache-dir -r requirements-runtime.lock \
    && groupadd --gid 10001 detective \
    && useradd --uid 10001 --gid 10001 --no-create-home detective
COPY pyproject.toml alembic.ini /app/
COPY backend /app/backend
RUN python -m pip install --no-build-isolation --no-deps . \
    && mkdir -p /data/cases && chown -R 10001:10001 /data
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "detective.api", "--host", "0.0.0.0", "--port", "8000"]
