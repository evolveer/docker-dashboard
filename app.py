import json
import re
import threading
import time
from collections import deque
from datetime import datetime

import docker
from docker.transport.unixconn import UnixHTTPAdapter
from flask import Flask, Response, jsonify, request, stream_with_context

app = Flask(__name__)


def patch_requests_docker_compat():
    if "get_connection_with_tls_context" in UnixHTTPAdapter.__dict__:
        return

    if getattr(UnixHTTPAdapter, "_requests_232_compat", False):
        return

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        return self.get_connection(request.url, proxies=proxies)

    # requests 2.32+ now calls get_connection_with_tls_context() instead of
    # get_connection(). Older docker SDK releases only override the latter for
    # the unix socket transport, which breaks Docker access on Linux.
    UnixHTTPAdapter.get_connection_with_tls_context = get_connection_with_tls_context
    UnixHTTPAdapter._requests_232_compat = True


patch_requests_docker_compat()


MAX_EVENTS = 500
_events = deque(maxlen=MAX_EVENTS)
_events_lock = threading.Lock()
_tailing = set()
_tailing_lock = threading.Lock()

HTTP_REQUEST_RE = re.compile(
    r'(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|CONNECT)\s+'
    r'(?P<path>/\S*)\s+HTTP/[\d.]+.*?(?P<status>\d{3})?',
    re.IGNORECASE,
)
COMBINED_LOG_RE = re.compile(
    r'(?P<client>[\d\.]+|[\w:]+)\s+.*?"(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+'
    r'(?P<path>\S+)\s+HTTP/[\d.]+"\s+(?P<status>\d{3})\s+(?P<bytes>\d+|-)'
    r'(?:\s+"(?P<referer>[^"]*)"\s+"(?P<ua>[^"]*)")?',
    re.IGNORECASE,
)
UVICORN_RE = re.compile(
    r'(?P<client>[\d\.]+:\d+)\s+-\s+"(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+'
    r'(?P<path>\S+)\s+HTTP/[\d.]+"\s+(?P<status>\d{3})',
    re.IGNORECASE,
)


def get_docker_client():
    try:
        client = docker.from_env()
        client.ping()
        return client, None
    except Exception as e:
        return None, str(e)


def format_bytes(size):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def format_uptime(started_at_str):
    try:
        # Docker returns ISO 8601 with nanoseconds, trim to microseconds
        ts = started_at_str[:26] + "Z"
        started = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")
        delta = datetime.utcnow() - started
        total_seconds = int(delta.total_seconds())
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        if hours > 0:
            return f"{hours}h {minutes}m {seconds}s"
        if minutes > 0:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"
    except Exception:
        return "N/A"


def _parse_log_line(container_name, container_id, line):
    for pattern in (COMBINED_LOG_RE, UVICORN_RE, HTTP_REQUEST_RE):
        match = pattern.search(line)
        if not match:
            continue

        data = match.groupdict()
        status = int(data.get("status") or 0)
        return {
            "ts": time.time(),
            "container": container_name,
            "cid": container_id,
            "method": (data.get("method") or "").upper(),
            "path": data.get("path") or "/",
            "status": status,
            "client": data.get("client") or "",
            "bytes": data.get("bytes") or "-",
            "ua": (data.get("ua") or "")[:80],
            "raw": line.strip()[:200],
        }
    return None


def _tail_container(container_id, container_name):
    try:
        client, error = get_docker_client()
        if error:
            return

        container = client.containers.get(container_id)
        for raw in container.logs(stream=True, follow=True, tail=0, timestamps=False):
            try:
                line = raw.decode("utf-8", errors="replace")
            except Exception:
                continue

            event = _parse_log_line(container_name, container_id[:12], line)
            if event:
                with _events_lock:
                    _events.append(event)
    except Exception:
        pass


@app.route("/")
def index():
    with open("./templates/index.html", "r") as f:
        return f.read()


@app.route("/api/containers", methods=["GET"])
def list_containers():
    client, error = get_docker_client()
    if error:
        return jsonify({"error": error}), 500

    show_all = request.args.get("all", "false").lower() == "true"
    try:
        containers = client.containers.list(all=show_all)
        result = []
        for c in containers:
            c.reload()
            attrs = c.attrs
            network_settings = attrs.get("NetworkSettings", {})
            networks = network_settings.get("Networks", {})
            ports = network_settings.get("Ports", {})
            port_list = []
            for container_port, bindings in ports.items():
                if bindings:
                    for binding in bindings:
                        port_list.append(f"{binding['HostPort']}->{container_port}")
                else:
                    port_list.append(container_port)

            started_at = attrs.get("State", {}).get("StartedAt", "")
            uptime = format_uptime(started_at) if c.status == "running" else "-"

            result.append(
                {
                    "id": c.short_id,
                    "full_id": c.id,
                    "name": c.name,
                    "image": c.image.tags[0] if c.image.tags else c.image.short_id,
                    "status": c.status,
                    "uptime": uptime,
                    "ports": port_list,
                    "networks": list(networks.keys()),
                }
            )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/containers/<container_id>/stop", methods=["POST"])
def stop_container(container_id):
    client, error = get_docker_client()
    if error:
        return jsonify({"error": error}), 500
    try:
        container = client.containers.get(container_id)
        container.stop()
        return jsonify({"success": True, "message": f"Container {container.name} stopped."})
    except docker.errors.NotFound:
        return jsonify({"error": "Container not found."}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/containers/<container_id>/start", methods=["POST"])
def start_container(container_id):
    client, error = get_docker_client()
    if error:
        return jsonify({"error": error}), 500
    try:
        container = client.containers.get(container_id)
        container.start()
        return jsonify({"success": True, "message": f"Container {container.name} started."})
    except docker.errors.NotFound:
        return jsonify({"error": "Container not found."}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/containers/<container_id>/restart", methods=["POST"])
def restart_container(container_id):
    client, error = get_docker_client()
    if error:
        return jsonify({"error": error}), 500
    try:
        container = client.containers.get(container_id)
        container.restart()
        return jsonify({"success": True, "message": f"Container {container.name} restarted."})
    except docker.errors.NotFound:
        return jsonify({"error": "Container not found."}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/containers/<container_id>/remove", methods=["DELETE"])
def remove_container(container_id):
    client, error = get_docker_client()
    if error:
        return jsonify({"error": error}), 500
    try:
        container = client.containers.get(container_id)
        container.remove(force=True)
        return jsonify({"success": True, "message": "Container removed."})
    except docker.errors.NotFound:
        return jsonify({"error": "Container not found."}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/containers/<container_id>/logs", methods=["GET"])
def container_logs(container_id):
    client, error = get_docker_client()
    if error:
        return jsonify({"error": error}), 500
    try:
        container = client.containers.get(container_id)
        logs = container.logs(tail=200, timestamps=True).decode("utf-8", errors="replace")
        return jsonify({"logs": logs})
    except docker.errors.NotFound:
        return jsonify({"error": "Container not found."}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/info", methods=["GET"])
def docker_info():
    client, error = get_docker_client()
    if error:
        return jsonify({"error": error}), 500
    try:
        info = client.info()
        return jsonify(
            {
                "containers": info.get("Containers", 0),
                "containers_running": info.get("ContainersRunning", 0),
                "containers_paused": info.get("ContainersPaused", 0),
                "containers_stopped": info.get("ContainersStopped", 0),
                "images": info.get("Images", 0),
                "docker_version": info.get("ServerVersion", "N/A"),
                "os": info.get("OperatingSystem", "N/A"),
                "memory": format_bytes(info.get("MemTotal", 0)),
                "cpus": info.get("NCPU", 0),
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/networks", methods=["GET"])
def list_networks():
    client, error = get_docker_client()
    if error:
        return jsonify({"error": error}), 500

    try:
        networks = client.networks.list()
        containers = client.containers.list(all=True)

        nodes = []
        edges = []
        seen_networks = set()

        for container in containers:
            container.reload()
            attached_networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
            nodes.append(
                {
                    "id": f"c_{container.short_id}",
                    "type": "container",
                    "label": container.name,
                    "status": container.status,
                    "image": container.image.tags[0] if container.image.tags else container.image.short_id,
                    "short_id": container.short_id,
                }
            )
            for network_name, network_info in attached_networks.items():
                network_id = f"n_{network_name}"
                seen_networks.add(network_id)
                edges.append(
                    {
                        "source": f"c_{container.short_id}",
                        "target": network_id,
                        "ip": network_info.get("IPAddress", ""),
                    }
                )

        for network in networks:
            network_id = f"n_{network.name}"
            nodes.append(
                {
                    "id": network_id,
                    "type": "network",
                    "label": network.name,
                    "driver": network.attrs.get("Driver", "bridge"),
                    "internal": network.attrs.get("Internal", False),
                    "scope": network.attrs.get("Scope", "local"),
                }
            )
            seen_networks.add(network_id)

        return jsonify({"nodes": nodes, "edges": edges})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/monitor/start", methods=["POST"])
def monitor_start():
    client, error = get_docker_client()
    if error:
        return jsonify({"error": error}), 500

    try:
        started = []
        for container in client.containers.list():
            with _tailing_lock:
                if container.id in _tailing:
                    continue
                _tailing.add(container.id)

            thread = threading.Thread(
                target=_tail_container,
                args=(container.id, container.name),
                daemon=True,
            )
            thread.start()
            started.append(container.name)

        with _tailing_lock:
            total_watching = len(_tailing)

        return jsonify({"started": started, "total_watching": total_watching})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/monitor/events", methods=["GET"])
def monitor_events():
    since = float(request.args.get("since", 0))
    limit = int(request.args.get("limit", 200))
    container_filter = request.args.get("container", "").lower()

    with _events_lock:
        events = list(_events)

    events = [event for event in events if event["ts"] > since]
    if container_filter:
        events = [event for event in events if container_filter in event["container"].lower()]

    events = sorted(events, key=lambda event: event["ts"], reverse=True)[:limit]
    return jsonify(events)


@app.route("/api/monitor/stream")
def monitor_stream():
    def generate():
        last_ts = time.time()
        yield 'data: {"ping": true}\n\n'
        while True:
            time.sleep(0.5)
            with _events_lock:
                new_events = [event for event in _events if event["ts"] > last_ts]

            if new_events:
                last_ts = max(event["ts"] for event in new_events)
                for event in sorted(new_events, key=lambda item: item["ts"]):
                    yield f"data: {json.dumps(event)}\n\n"
            else:
                yield ": keep-alive\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/monitor/clear", methods=["POST"])
def monitor_clear():
    with _events_lock:
        _events.clear()
    return jsonify({"success": True})


@app.route("/api/monitor/status", methods=["GET"])
def monitor_status():
    with _tailing_lock:
        watching = len(_tailing)
    with _events_lock:
        buffered_events = len(_events)
    return jsonify({"watching": watching, "buffered_events": buffered_events})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
