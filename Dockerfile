FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY data ./data

EXPOSE 6278

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:6278/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "zcaisagent.api.app:app", "--host", "0.0.0.0", "--port", "6278"]
