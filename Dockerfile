FROM python:3.11-slim

# Install ffmpeg for thumbnail generation
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py .
COPY templates ./templates
# COPY static ./static

# Create directory for videos
RUN mkdir -p /app/videos

EXPOSE 5000

CMD ["python", "app.py"]
