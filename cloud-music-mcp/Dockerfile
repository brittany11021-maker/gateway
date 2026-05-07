FROM python:3.11-slim
WORKDIR /app

RUN pip install --no-cache-dir \
    "fastmcp>=2.0.0" \
    "mcp[cli]>=1.2.0" \
    "requests>=2.31.0"

# pyncm is bundled in src/pyncm (not on PyPI)
COPY src /app/src
ENV PYTHONPATH=/app/src

# Pre-bake cookies (login state carried from local machine)
# Cookies are in src/cloud_music_mcp/storage/cookies.json (already copied)

EXPOSE 3001
CMD ["python", "-m", "cloud_music_mcp.server"]
