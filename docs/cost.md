# What agentix costs on Modal

`agentix cost` shows this month's Modal spend of agentix: what Modal billed
the `agentix` app (and the `agentix-runner` app of detached runs), and an
estimate of how that splits by sandbox name, image builds, and other sandboxes
without a name. Every number says where it comes from, and every estimate is
marked `~`. `agentix cost --json` prints the same numbers as JSON.

The command only reads. It creates no app, Volume or Dict (lookups do not pass
`create_if_missing`), writes no record and no cache entry, starts no sandbox,
and needs no credentials beyond the Modal token that the other commands use.

This note records what Modal exposes, as checked on 2026-11-15 with the
`modal` package 1.6.4 and Modal's public docs.

## What Modal exposes

| API | What it gives | Reachable with this token |
| --- | --- | --- |
| `modal.Workspace.from_context().billing.report(start, end, resolution, tag_names)` | Cost per object per interval, split by resource (`CPU`, `Memory`, `Network Egress`, GPU types) | Yes |
| `...billing.summary(cycle)` | One month's metered cost by category, adjustments (credits, free storage), billed cost | Yes |
| `...billing.rates()` | The workspace's current prices, including `cpu_hour_cost_sandbox` and `mem_gib_hour_cost_sandbox` | Yes |
| `modal.billing.workspace_billing_report(...)` | The older form of `billing.report`; deprecated on 2026-06-18 in its favour | Not used |
| `modal billing report` / `modal billing summary` | The same data from Modal's CLI | Not used |
| `Sandbox._experimental_stats()` | One sandbox's runtime, CPU and memory use, and its cost at the workspace's rates | No: `PermissionDeniedError: Sandbox stats are not enabled for this workspace` |
| `Sandbox.list(app_id=..., tags=...)` | Running sandboxes only (`include_finished=False` is fixed) | Yes |
| `SandboxList` RPC with `include_finished=True` | Ended sandboxes too, with start and finish times, CPU and memory requested, and tags | Yes, but it is Modal's internal API (`Client.stub` warns that it can change at any time) |

Modal's guide on billing says the programmatic billing APIs are for the Team
and Enterprise plans. The workspace that was checked could call them.

### What the billing report can attribute

- Rows name **apps**: every row seen had an app ID (`ap-...`) as `object_id`
  and the app's name as `description`. There are no rows per sandbox, per
  image build, or per snapshot. All sandboxes of agentix, the image builder
  sandbox, and `flake update` run in the one `agentix` app, so Modal's own
  numbers stop at the app.
- `tag_names=["*"]` returned no tags. Report tags are app tags
  (`App.set_tags`); the `agentix-name` tag that agentix puts on each named
  sandbox does not reach the report.
- An hourly report may span at most 7 days (`InvalidError: Billing report
  range limit exceeded`). `agentix cost` therefore asks for whole days of the
  month at daily resolution and for today at hourly resolution. The hourly
  report included the hour in progress.

### What the monthly summary shows

Its metered cost splits into `Deployed Apps`, `Ephemeral Apps`, `Volumes`,
`Network Egress`, and `LLM Tokens`, and its adjustments into `Credits`, `Free
Storage`, `Network Egress Allowance`, `Reservation Adjustment`, and `Plan
Cost`. There is **no line for snapshot or image storage**, so Modal does not
meter it (or not visibly). `agentix cost` reports it as not metered, and only
counts snapshots. The summary covers the whole workspace, so `agentix cost`
does not show it.

### Snapshots and images

Modal reports no size for a snapshot or image (no size field in the image
messages of `modal_proto`), cannot list snapshots, and does not bill them on a
separate line. Filesystem and directory snapshots expire after 30 days unless
given another TTL. A filesystem snapshot holds only what differs from its base
image. The unpacked image roots that `image build` keeps (`agentix-rootfs:*`,
about 2 GB each, `ttl=None`) are images too, and cannot be listed or counted
from Modal either.

## Prices

The one price table is `PRICES` in `src/agentix/cost.py`, from
<https://modal.com/pricing>, checked on 2026-11-15. Sandboxes are billed at
the non-preemptible rate, three times the list price of functions:

| Resource | Sandbox price | Note |
| --- | --- | --- |
| CPU | $0.00003942 per physical core per second | at least 0.125 core per container |
| Memory | $0.00000667 per GiB per second | |
| GPU | standard GPU rates | agentix asks for none |
| Volumes | $0.09 per GiB per month | 1 TiB a month free |
| Network egress | $0.04 per GiB | |

`agentix cost` prefers the workspace's own rates from `billing.rates()`
(`cpu_hour_cost_sandbox` 0.1419 and `mem_gib_hour_cost_sandbox` 0.024 per hour,
the same prices) and falls back to the table when they cannot be read. The
output says which it used. Region selection would multiply prices by 1.15 to
1.75; agentix selects no region.

## How the estimate works

For each sandbox, `agentix cost` takes when it ran this month and what it
asked for, and prices `max(cpu, 0.125) cores x seconds` and `memory GiB x
seconds`. Each row's SOURCE says where its runtimes came from:

- `modal`: start, finish (or now, while it runs), CPU and memory from Modal's
  listing of ended sandboxes. That listing reached back about two days when it
  was checked (on 2026-11-15, to 2026-11-13 18:22 UTC); sandboxes older than
  that are found only through the records.
- `record`: start from the agentix record of the name, end from the exit
  snapshot it recorded for that sandbox, CPU and memory from the name's
  options.
- `record-timeout`: a recorded sandbox with no exit snapshot is counted up to
  its timeout. This is an upper bound.

A sandbox is put under its `agentix-name` tag or its record's name. One with
neither is `(image builds)` when it has the builder's 8 CPU and 16384 MiB
(`BUILD_CPU`, `BUILD_MEMORY_MIB` in `image.py`), and `(unnamed)` otherwise:
`sandbox up --ephemeral`, `flake update`, and other scripts that use the app.
Image layers that Modal builds itself (the checks in `image build`) do not run
in a sandbox and are not estimated.

The line "Not attributed to a row above" is what Modal billed the apps minus
the sum of the estimates: image layer builds, egress, the runner app,
sandboxes older than the listing that have no record, and the error of the
estimate.

The estimate counts what a sandbox asked for. Modal bills, for each usage
sample, the greater of the measured and the requested CPU and memory (the
docstring of `Sandbox._experimental_stats` says so), so a sandbox that used
more than it asked for cost more than its estimate.

## Unverified

- Whether Modal bills image build layers or snapshot storage at all: neither
  appears as its own line anywhere.
- How far back Modal's listing of ended sandboxes reaches in general; two days
  is what one workspace showed once.
- Whether the billing APIs work on the Starter plan; Modal's guide says they
  are for Team and Enterprise. When the report cannot be read, `agentix cost`
  still prints the estimates and says why the billed total is missing.
