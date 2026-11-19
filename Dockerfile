FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NC_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY nextcommand ./nextcommand
COPY static ./static
RUN useradd -r -u 10001 nc && mkdir /data && chown nc /data
USER nc
VOLUME /data
EXPOSE 9054
CMD ["uvicorn", "--factory", "nextcommand.api:app_from_env", "--host", "0.0.0.0", "--port", "9054", "--workers", "1", "--proxy-headers"]
