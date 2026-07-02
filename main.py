"""
main.py  --  SQL Server Blocking Agent (Microsoft Agent Framework)
------------------------------------------------------------------
Starts the polling loop.
Each cycle runs the Detection -> Analyzer -> Determination ->
[Action] -> RCA -> Notification MAF workflow.

Usage:
    .venv\\Scripts\\python main.py
"""

import asyncio
import sys
from pathlib import Path
import logging
import os
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv

# Load .env BEFORE importing agents (they construct the LLM client at
# class-definition time and need FOUNDRY_PROJECT_ENDPOINT + LLM_API_KEY).
load_dotenv()

# Add src/ to path so all package imports resolve without a src. prefix
sys.path.insert(0, str(Path(__file__).parent / "src"))

from orchestrator.config   import load_config
from orchestrator.state    import BlockingState
from orchestrator.workflow import AGENT_WORKFLOW

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


# ── One cycle ─────────────────────────────────────────────────────────────────

async def run_cycle(config: dict) -> BlockingState:
    """Run one complete observe -> reason -> act -> log cycle."""

    correlation_id = str(uuid.uuid4())[:8]

    # Build the initial state -- blank slate for this cycle.
    initial_state = BlockingState(
        server_name=config["server_name"],
        correlation_id=correlation_id,
        dry_run=config["dry_run"],
        cycle_start_utc=datetime.now(timezone.utc).isoformat(),
    )

    log.info("--- Cycle %s @ %s ---", correlation_id,
             datetime.now(timezone.utc).strftime("%H:%M:%S UTC"))

    # AGENT_WORKFLOW.run() returns the BlockingState yielded by notification_node
    final: BlockingState = await AGENT_WORKFLOW.run(initial_state)

    if final is None:
        # Should not happen -- notification_node always calls yield_output
        log.warning("Cycle %s: workflow returned no output state", correlation_id)
        return initial_state

    log.info(
        "Cycle %s done: decision=%-10s  kill=%s  risk=%-8s  errors=%d",
        correlation_id,
        final.get("decision", "SKIP"),
        final.get("kill_status", "NOT_ATTEMPTED"),
        final.get("risk_level", "LOW"),
        len(final.get("errors", [])),
    )

    if final.errors:
        for err in final.errors:
            log.warning("  Error: %s", err)

    return final


# ── Main loop ─────────────────────────────────────────────────────────────────

async def main():
    log.info("=" * 60)
    log.info("SQL Server Blocking Agent (Microsoft Agent Framework)  --  starting up")
    log.info("=" * 60)

    if not os.getenv("FOUNDRY_PROJECT_ENDPOINT") and not os.getenv("LLM_BASE_URL"):
        log.error(
            "FOUNDRY_PROJECT_ENDPOINT is not set in .env  --  "
            "copy .env.example to .env, fill in the endpoint, and restart."
        )
        return

    config = load_config()

    log.info("")
    log.info("Press Ctrl+C to stop.")
    log.info("")

    poll_seconds = config["poll_interval_seconds"]

    try:
        while True:
            try:
                await run_cycle(config)
            except Exception as e:
                log.error("Cycle crashed: %s", e, exc_info=True)

            log.info("Sleeping %ss until next cycle...\n", poll_seconds)
            await asyncio.sleep(poll_seconds)

    except KeyboardInterrupt:
        log.info("Stopped by user.")


if __name__ == "__main__":
    asyncio.run(main())
