"""o11yag load generator.

Sends support tickets at the orchestrator so the stack produces continuous
telemetry. Emits no telemetry of its own — it is a traffic source, not part of
the architecture being demonstrated.
"""
import logging
import random
import signal
import time
import uuid

import requests

import tickets
from config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("loadgen")

_running = True


def _stop(signum, _frame):
    global _running
    log.info("signal %s received, finishing the current ticket then exiting", signum)
    _running = False


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("sending to %s every %ss", Config.ORCHESTRATOR_URL, Config.INTERVAL_S)

    while _running:
        customer_id, text = random.choice(tickets.POPULATION)
        payload = {"ticket_id": f"TK-{uuid.uuid4().hex[:8]}",
                   "customer_id": customer_id, "tenant": Config.TENANT, "text": text}
        try:
            r = requests.post(f"{Config.ORCHESTRATOR_URL}/chat",
                              json=payload, timeout=Config.TIMEOUT_S)
            body = r.json()
            log.info("%s [%s] %s -> %s (%s steps of LLM, %s tokens, %.0fms)",
                     payload["ticket_id"], body.get("intent"), text[:48],
                     body.get("outcome"), body.get("llm_calls"), body.get("tokens"),
                     body.get("latency_ms", 0))
            if Config.LOG_RESPONSES:
                log.info("   answer: %s", str(body.get("answer", ""))[:200])
        except Exception as exc:
            log.warning("%s failed: %s", payload["ticket_id"], exc)

        # Sleep in slices so SIGTERM doesn't wait out a whole interval.
        slept = 0.0
        while _running and slept < Config.INTERVAL_S:
            time.sleep(0.5)
            slept += 0.5

    log.info("stopped")


if __name__ == "__main__":
    main()
