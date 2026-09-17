"""Gunicorn configuration. Env-driven so one image behaves across environments."""
import os

bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
workers = int(os.getenv("WEB_CONCURRENCY", "4"))

# Model calls are slow and a ticket may wait on a human approval downstream, so
# the worker timeout must exceed WORKER_TIMEOUT_S or gunicorn hard-kills the
# worker mid-ticket and the request dies with no useful error.
timeout = int(os.getenv("GUNICORN_TIMEOUT", "360"))
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
