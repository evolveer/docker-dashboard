# Docker Dashboard

A lightweight, dark-themed web UI for managing Docker containers on a Linux system.

## Features

- **Live container list** — shows all running containers with name, image, status, uptime, and ports
- **Stop / Start / Restart** — one-click container lifecycle management with confirmation dialogs
- **Remove** — force-remove containers with a single click
- **View Logs** — tail the last 200 lines of any container's logs in a modal
- **Search** — filter containers by name, image, or ID in real time
- **Stats bar** — shows running/stopped counts, total containers, images, Docker version, and host resources
- **Auto-refresh** — polls every 5 seconds (toggle on/off)
- **Toast notifications** — success/error feedback for every action

## Requirements

- Python 3.7+
- Docker installed and running
- Your user must have access to the Docker socket

## Quick Start

```bash
# 1. Install dependencies
pip3 install --upgrade flask docker requests

# 2. Run the dashboard
cd docker-dashboard
python3 app.py

# 3. Open in your browser
# http://localhost:5050
```

Or use the included startup script:

```bash
./start.sh
```

If your Python environment already had an older Docker SDK installed, rerun the
install command above with `--upgrade`. This project also includes a runtime
compatibility shim for the `requests 2.32+` transport change that broke older
Docker SDK unix-socket adapters on Linux.

## Docker Socket Permissions

If you get a "Permission denied" error connecting to Docker, add your user to the `docker` group:

```bash
sudo usermod -aG docker $USER
# Log out and back in for the change to take effect
```

## File Structure

```
docker-dashboard/
├── app.py              # Flask backend + Docker SDK API
├── start.sh            # Convenience startup script
├── README.md
└── templates/
    └── index.html      # Single-page frontend (HTML/CSS/JS)
```

## API Endpoints

| Method   | Endpoint                              | Description              |
|----------|---------------------------------------|--------------------------|
| GET      | `/`                                   | Serve the dashboard UI   |
| GET      | `/api/info`                           | Docker host info/stats   |
| GET      | `/api/containers?all=true`            | List containers          |
| POST     | `/api/containers/<id>/stop`           | Stop a container         |
| POST     | `/api/containers/<id>/start`          | Start a container        |
| POST     | `/api/containers/<id>/restart`        | Restart a container      |
| DELETE   | `/api/containers/<id>/remove`         | Force-remove a container |
| GET      | `/api/containers/<id>/logs`           | Get last 200 log lines   |
