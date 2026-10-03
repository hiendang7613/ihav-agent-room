---
name: doctor
description: Inspect local dependencies without starting agents or changing settings.
disable-model-invocation: true
---

Run `ihav-agent-room --json doctor`. Reject arguments. Explain failed capability checks and
their effect on using the team and the smallest concrete next step. Summarize readiness in ordinary
language; keep the full diagnostic available on request. This does not prove login, peer delivery, model response or
permission handling. Do not install dependencies, alter credentials, widen permissions,
or perform a provider test automatically.
