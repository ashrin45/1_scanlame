FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git libglib2.0-0 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi uvicorn python-multipart opencv-python-headless numpy
RUN useradd -m -u 1000 user
WORKDIR /app
COPY --chown=user . /app
USER user
ENV SCANLAME_DATA=/tmp/series
EXPOSE 7860
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
