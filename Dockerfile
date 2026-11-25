# Small, non-root, and no build toolchain in the final image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=prepub.settings

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Coordinate files are written here. It is a volume in deployment: an embargoed file must survive
# a container restart, and must not be baked into an image that gets pushed to a registry.
RUN mkdir -p /app/media /app/staticfiles \
 && DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput

RUN useradd --create-home --uid 10001 prepub \
 && chown -R prepub:prepub /app/media /app/staticfiles
USER prepub

EXPOSE 8000

# Two workers by default; the service is I/O bound on the database and the filesystem.
CMD ["gunicorn", "prepub.wsgi:application", "--bind", "0.0.0.0:8000", \
     "--workers", "2", "--access-logfile", "-"]
