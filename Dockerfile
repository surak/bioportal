FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# psycopg[binary] and cryptography ship wheels, so no build toolchain is needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Collected at build time so the web container starts read-only-friendly.
# A throwaway key is fine here: nothing secret is baked in, and the real
# settings arrive from the environment at runtime.
RUN DJANGO_SECRET_KEY=build-only DJANGO_DEBUG=0 DJANGO_ALLOWED_HOSTS=localhost \
    python manage.py collectstatic --noinput

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data/artifacts && chown -R app:app /data
USER 10001

EXPOSE 8000
CMD ["gunicorn", "bioportal.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
