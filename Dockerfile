FROM python:3.12-slim
WORKDIR /app
COPY app/ /app/
# A new named volume starts with this folder's owner, so the app (nobody) can write to it.
RUN mkdir /data && chown nobody /data
USER nobody
EXPOSE 8000
# Deproi decides between success and rollback from this health check.
HEALTHCHECK --interval=2s --timeout=2s --start-period=2s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]
CMD ["python", "-u", "server.py"]
