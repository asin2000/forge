# Readiness Orchestrator (manager agent)
Decomposes NMC events into exclusively-owned work packages (ORC-1); performs no
domain work (ORC-2); holds the Workforce reserve and reassigns atomically on
failure (ORC-3/ORC-4); owns workflow state + Logical Clock due events (ORC-5).
Cloud Run service `forge-orchestrator`, SA `forge-orchestrator-sa`. Lands Day 3.
