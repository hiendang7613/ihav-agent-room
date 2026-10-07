# Qualified catalog update adapters

Decision D-ccc57589 approves one supervisor updating qualified ihav releases.
Decision D-2c743e67 keeps the independent fleet tool in the catalog project.
This source slice supplies adapters and an attempt journal for that tool. It
does not run updates, install a release, activate a pointer or restart a session.
The current supervisor continues to announce catalog changes only.

`catalogwatch.read_catalog_snapshot` retains both host catalogs at the same Git
commit, including source URL, subdirectory, tag and SHA. The existing
`read_catalog` tuple and data-only release notices remain supported. A new
snapshot can add metadata to an already observed commit without another notice.
Reading a catalog does not qualify it for installation.

A nonblocking machine file lock now covers the entire cooperating watcher scan,
including the catalog read, notice and snapshot write. An expired timing lease
cannot let another cooperating scan overwrite a newer snapshot. Older resident
watcher code does not use this lock; offline checks do not establish its adoption.
Notices stay within the machine ledger's 8192-byte UTF-8 limit and explicitly say
when shortened. The full available snapshot remains in `catalog_seen:<url>`;
missing or unreadable Codex metadata stays explicitly `null` and holds updates.
Each notice includes a deterministic URL/before-head/after-head transition marker.
If its entry committed before snapshot persistence was interrupted, the next
cooperating scan recognizes that committed marker and saves the snapshot without
posting again. Existing queue import recovers delivery of the original entry.
This recovery applies to newly marked notices, not unmarked historical entries
or concurrent older resident code. It does not reconcile native installer effects.

`catalogupdate.plan_update` takes four independent evidence objects plus the
plugin name and host:

- A snapshot with `url`, `head` and both host `catalogs`.
- A verified owner qualification bound to that URL and commit, a successful
  catalog-check artifact SHA256, resolved tag commit, payload SHA256 and both
  host manifest versions. Routine releases also need a completed, passing
  one-hour canary for that exact source in the Agent Room room.
- The current selected installation, including its source identity, user scope,
  enabled state, payload SHA256 and preserved rollback inventory path/digest.
- Owner policy with explicit `enabled: true` and the exact `catalog_url`.
  No policy is enabled by default. Shared activation history is a separate
  `last_activation` object with catalog `head` and Unix timestamp `at`.

The qualification object is a contract for a trusted owning coordinator after
it verifies the original artifacts. An arbitrary JSON flag, catalog entry,
peer notice or successful CLI exit is not that verification and supplies no
admin or native authority. The journal's digests protect identity comparisons;
they are not signatures or an authorization mechanism.

Held plans contain a reason and no commands. Examples include unmatched host
pins, unpinned branches, command sources, missing qualification, local/development
marketplaces, disabled/project/managed selections, downgrades, version collisions,
missing rollback evidence, incomplete canaries and daily cadence holds. Security
and blocking fixes skip cadence only; every other eligibility condition remains.
Routine releases from the same catalog batch may update both hosts/plugins in
that batch. A different batch waits at least 86400 seconds.

The installed Claude help supports these two named commands:

```
claude plugin marketplace update ihav --json
claude plugin update PLUGIN@ihav --scope user --json
```

The initial plan exposes only the marketplace refresh command. The owning driver
must reserve intent before that first native mutation. After refresh it must
independently read and supply the exact qualified head and both source entries
to `plugin_update_command`. Only a matching snapshot produces the plugin update
command. The driver must preserve all loaded cache roots and exact executable
modes, recheck native permissions and config ownership,
and independently verify the installed payload. A refresh may move the catalog;
the driver must hold rather than update an unqualified new revision. No adapter
adds `--yes`, accepts a marketplace command, disables a guard or changes credentials.

The installed Codex help documents marketplace `upgrade` and plugin `add`, but
does not document updating an already installed plugin. The adapter returns
`supported_plugin_update_unverified`; it does not invent a remove/add sequence.
An actual supported existing-plugin updater contract and its preservation tests
are still prerequisites for implementing the Codex effect.

`UpdateJournal` uses `GlobalSpace`'s existing private machine SQLite ledger.
`begin` atomically records one intent and a machine-wide active reservation.
The intent identity includes host, selector and exact payload/source identity;
an unrelated catalog commit cannot cause the same payload to run again. Shared
cadence is rechecked inside the transaction before a new intent is recorded.

`finish` requires the reservation token, command outcome and an independently
verified postimage matching the target. Zero exit alone, a timeout, nonzero exit,
missing output or a mismatched payload keeps the outcome `unknown` and retains
the machine hold. A new process observes that hold; it does not replay or erase
the attempt. Owning reconciliation of unknown effects remains required. No
automatic rollback or retry exists in this slice.

Even a matching installed result reports `loaded: false`. Installer output,
on-disk identity, execution from an existing session and newly completed native
work are different observations. Actual update dispatch, retention/restoration,
driver scheduling and generic session adoption remain outside this bounded
source slice and are not completed by its offline tests.
