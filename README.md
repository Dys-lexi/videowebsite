# Video Gallery

A simple Docker-based video gallery that displays videos grouped by creation date with thumbnail previews.

## Features

- Automatically scans video directory
- Groups videos by creation date
- Generates thumbnails on-the-fly (no caching)
- Clean, modern web interface
- Supports multiple video formats (mp4, avi, mkv, mov, wmv, flv, webm, m4v)
- Click to play videos in a modal player

## Quick Start

1. Put your videos in the `videos/` directory
2. Run:
   ```bash
   docker-compose up -d
   ```
3. Open http://localhost:5000 in your browser

## Directory Structure

```
videoserve/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── app.py
├── templates/
│   └── index.html
└── videos/          # Put your videos here
```

## Configuration

Edit `docker-compose.yml` to change:
- Port mapping (default: 5000)
- Video directory location

## Development

To run without Docker:
```bash
pip install -r requirements.txt
python app.py
```

Note: Requires ffmpeg to be installed on your system.
