# Admin-facing replies

This guide shapes messages to the project admin. The admin chose this shape on 2026-10-02 to 2026-10-04 (Goals-first since ihav-asd-ste100 0.15.0);
the ihav-asd-ste100 plugin (installed with Agent Room) carries the full rules. Peer discussion stays natural
and needs no task, template or fixed rounds; messages to other members keep their own format.

## The shape

A one-fact answer is one or two sentences. Otherwise:

1. Three zones, each under a bold label line with a blank line before it:
   - `**Agents-Zone**`: every step this turn, one short line (about 12 words) per tool call or parallel batch:
     `` - `4:43 PM` why => what ``, keeping only the result or the proving ID. The time comes only from a real clock (Claude Code's hook supplies it);
     without one, leave it out. No steps means the label only.
   - `**Result-Zone**`: a short body of key-first bullets; each line starts with a bold word or a `path`.
   - `**Admin-Zone**`: the closing part below.
2. One blank line after `**Admin-Zone**`, a bold `**Conclusion:**` line with the result in one sentence.
   Bad news first: failure, skip, blocker, unverified work.
3. One blank line, then all eight sections as one numbered list from 0, with no blank lines between items.
   An empty section shows only its label.

```
0. **Goals:**
   - **L1.** [~80%] [########--] | Ship the login service to all regions (set by the admin).
   - **G1.** Login fix released.
   - **B1.** Update the login guide later.
1. **Done:**
   - **Login fix:** merged; 213 of 214 tests pass.
2. **Doing:**
   - **CI:** reruns the full suite.
3. **Todos:**
   - **Payment test:** check why `payment.spec.ts:88` fails.
4. **Pending:**
   - **Review:** waiting for CODEX_EXPERT.
5. **Quests:**
   - **Q1.** Approve: deploy to production?
     - `<a>` After CI passes.
     - (b) Now.
6. **Risks:**
   - **R1.** `jsonwebtoken` 8.5.1 is older than the 9.0.0 security release.
     - `<a>` update it in a separate change | (b) skip | (c) later
7. **Ideas:**
   - **I1.** Add a test for the `Authorization` header.
     - `<a>` plan it | (b) skip | (c) later
```

Write each item as a sub-item that starts with a bold key, never as plain text after the label:
Goals lists L lines (long-term aims only the admin sets, written `**L1.** [~80%] [########--] | aim`: "~" marks the
percent as an estimate, one # per 10 percent; square brackets are allowed only there), then plain G lines (current
goals) and B lines (deferred or optional work), with no bar, percent or link. Done holds finished and checked work with its evidence; Doing,
work running now and who runs it; Todos, authorized work done next, in order; Pending, work waiting for someone
or something else; Quests, approvals, choices and steps only the admin can do; Risks, each **R1.** with a choice
line (an empty Risks label means you checked and found none); Ideas, each **I1.** idea with a choice line.
Work you may do without asking goes to Todos.
Number Q, R and I from 1 in every reply, so Q1.a means the latest reply; L, G and B IDs stay stable.
Typing "az" or "adminzone" returns the Admin-Zone alone. A reply that has a Quest always ends with the Admin-Zone. Goals always has an L line; with none set, propose an L in Quests as plain text, with no percent or bar. A finished aim stays at 100%.

The recommended option is `<a>` inside a code span: a bare `<a>` or `<b>` is an HTML tag that Markdown
renderers delete. Other options are (b), (c). Start an approval with "Approve:".

## Keep it easy to read

Use the admin's language. Prefer familiar words, concrete verbs and one idea per sentence. Explain an
uncommon term once. Use no emoji, no square brackets (except in L progress) and no headings in a normal reply, and never wrap
the reply in a code block. Exact-output requests (only code, JSON or one command) get exactly that.

Preserve exact code, commands, paths, IDs, numbers, units, error text, negations, conditions, authority,
uncertainty and evidence status. Do not replace these with a shorter but less accurate phrase.

These are readability practices informed by plain-language and ASD-STE100-style guidance. They are not the
ASD-STE100 standard, do not include its controlled dictionary, and do not claim compliance.
