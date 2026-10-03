---
name: stop
description: Stop this room's workers and persist manual stop until start.
disable-model-invocation: true
---

Run `ihav-agent-room --json stop`. Reject arguments. Preserve unfinished tasks and native history.
Report confirmed stopped, pending cleanup, or failed from the result. Never kill unrelated CLI
processes or stop a global daemon. Manual stop persists across reopening Claude until start.
Use ordinary language and explain that unfinished work is preserved. Give the smallest necessary
next step if cleanup is blocked; keep raw IDs/logs for diagnosis.
