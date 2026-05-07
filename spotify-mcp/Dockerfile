FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir \
    "fastmcp>=2.0.0" \
    "mcp[cli]>=1.2.0" \
    "httpx>=0.27.0"
COPY server.py .
EXPOSE 3002
CMD ["python", "server.py"]
