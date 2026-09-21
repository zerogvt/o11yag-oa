"""Gunicorn configuration. Env-driven so one image behaves across environments."""
import os

bind = f"0.0.0.0:{os.getenv('PORT', '8002')}"
workers = int(os.getenv("WEB_CONCURRENCY", "2"))

# Threads, not just more processes. Every long stretch of a ticket — the model
# calls and the approval poll loop — is a blocking HTTP wait, and a sync worker
# is held whole for its duration. With two sync workers, two tickets in flight
# leave nobody to answer /health: the liveness probe fails three times and
# Kubernetes restarts a pod that was merely busy. That is not hypothetical, it
# killed a refund one poll tick before the approval came back. Raising the
# probe's timeoutSeconds cannot fix it either, because the handler is not slow
# — it is never scheduled. gthread hands the request to a thread and leaves the
# worker free, so /health still answers while the loop is parked on someone
# else's ticket.
worker_class = os.getenv("GUNICORN_WORKER_CLASS", "gthread")
threads = int(os.getenv("GUNICORN_THREADS", "8"))

# A ticket here can sit through several model calls AND a human approval, so the
# worker timeout must exceed APPROVAL_TIMEOUT_S plus the loop's own time or
# gunicorn kills the worker while a reviewer is still deciding.
timeout = int(os.getenv("GUNICORN_TIMEOUT", "600"))
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

# Do NOT preload — the telemetry export threads must be created after the fork.
preload_app = False

accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOGLEVEL", "info")
