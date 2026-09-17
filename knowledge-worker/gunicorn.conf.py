"""Gunicorn configuration. Env-driven so one image behaves across environments."""
import os

bind = f"0.0.0.0:{os.getenv('PORT', '8001')}"
# Two workers, not four: each one runs the Qdrant seed on boot and holds its own
# model-gateway connection pool, and the throughput ceiling here is Ollama anyway.
workers = int(os.getenv("WEB_CONCURRENCY", "2"))

# Generation on a CPU-only model is slow; the worker timeout has to outlast it or
# gunicorn kills the worker mid-answer.
timeout = int(os.getenv("GUNICORN_TIMEOUT", "300"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.getenv("GUNICORN_KEEPALIVE", "5"))

# gunicorn 26 starts a control server whose socket defaults to
# $XDG_RUNTIME_DIR/gunicorn.ctl, falling back to $HOME/.gunicorn/gunicorn.ctl.
# Neither is writable here: the container runs with readOnlyRootFilesystem and
# only /tmp mounted writable, so the master logs
#   Control server error: [Errno 30] Read-only file system: '/home/appuser/.gunicorn'
# on every boot. Nothing in Kubernetes uses that socket — you scale and restart
# through kubectl — so turn it off rather than carve out another writable path.
control_socket_disable = True

# Do NOT preload. The telemetry SDKs run background export threads that must be
# created inside each worker after the fork; preload_app=True builds them in the
# master and they don't survive, silently breaking export.
preload_app = False

accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOGLEVEL", "info")
