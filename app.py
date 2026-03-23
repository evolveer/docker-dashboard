import docker
from docker.transport.unixconn import UnixHTTPAdapter
from flask import Flask, jsonify, request
from datetime import datetime

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
        elif hours > 0:
            return f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"
    except Exception:
        return "N/A"


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
            network_names = list(networks.keys())
            ports = network_settings.get("Ports", {})
            port_list = []
            for container_port, bindings in ports.items():
                if bindings:
                    for b in bindings:
                        port_list.append(f"{b['HostPort']}->{container_port}")
                else:
                    port_list.append(container_port)

            started_at = attrs.get("State", {}).get("StartedAt", "")
            uptime = format_uptime(started_at) if c.status == "running" else "-"

            result.append({
                "id": c.short_id,
                "full_id": c.id,
                "name": c.name,
                "image": c.image.tags[0] if c.image.tags else c.image.short_id,
                "status": c.status,
                "uptime": uptime,
                "ports": port_list,
                "networks": network_names,
            })
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
        return jsonify({"success": True, "message": f"Container removed."})
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
        return jsonify({
            "containers": info.get("Containers", 0),
            "containers_running": info.get("ContainersRunning", 0),
            "containers_paused": info.get("ContainersPaused", 0),
            "containers_stopped": info.get("ContainersStopped", 0),
            "images": info.get("Images", 0),
            "docker_version": info.get("ServerVersion", "N/A"),
            "os": info.get("OperatingSystem", "N/A"),
            "memory": format_bytes(info.get("MemTotal", 0)),
            "cpus": info.get("NCPU", 0),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=False)
