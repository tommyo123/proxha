# ProxHA reference

This document describes ProxHA in depth: cluster connections, placement rules,
balancing, capacity checks and maintenance integration. The README covers
installation and first-time setup.

Contents:

- [Cluster connections](#cluster-connections)
- [Placement rules](#placement-rules)
- [Balancing](#balancing)
- [Observed migration outcomes](#observed-migration-outcomes)
- [Balloon recovery](#balloon-recovery)
- [Native Proxmox HA integration](#native-proxmox-ha-integration)
- [Capacity and infrastructure](#capacity-and-infrastructure)
- [Container releases](#container-releases)

## Cluster connections

ProxHA connects to the Proxmox API with a user token. This is separate from the
username and password used to sign in to ProxHA itself.

### Create and save a connection

1. In Proxmox, create a dedicated user and API token under **Datacenter > Permissions**.
2. Grant `PVEAuditor` on `/` with propagation. If privilege separation is enabled,
   grant the necessary access to both the user and token.
3. In ProxHA, select **Connect cluster**. Enter a connection name, a node URL such
   as `https://prox01.example.com:8006`, the full token ID such as
   `proxha@pve!monitor`, and its secret.
4. Configure certificate verification as described below, then save.

The connection is tested before it is saved. Each saved cluster has its own
inventory, rules, balancing settings and history. Token secrets are encrypted
using the key in `secret.key`.

### Trust the Proxmox certificate

For certificates issued by a public CA, keep **Verify TLS certificate** enabled.
ProxHA uses the operating system's trust store.

For the default Proxmox cluster CA, you can paste its certificate:

1. Read `/etc/pve/pve-root-ca.pem` on a trusted cluster host.
2. In the ProxHA connection dialog, select **Paste CA certificate** and paste it.
3. Select **Show fingerprint**. Compare the displayed fingerprint with this command
   on the cluster host:

   ```sh
   openssl x509 -in /etc/pve/pve-root-ca.pem -noout -fingerprint -sha256
   ```

4. Select **Fingerprint matches**, then save the connection.

You can also select **Trust cluster CA** to fetch the certificate from the entered
node. If that node cannot yet be verified, clear **Verify TLS certificate** for
this initial read. The token is then sent to that address without TLS verification.
Compare the fingerprint on the host before confirming it. Saving the connection
tests the API again using the confirmed CA.

The CA must sign the node certificates, and the configured hostname must appear
in the node certificate. A custom or ACME node certificate may use a different CA.
The same verification applies when ProxHA switches to another node.

When the cluster CA changes, edit the connection, remove the old trusted CA and
trust the new one. Saving a new CA, token or API address resets endpoint discovery.

The HTTPS certificate served by the ProxHA web interface is independent of the
cluster CA. Configure it with the certificate options described in the README.

### Grant permissions for actions

Read access is enough to display the dashboard. Actions need additional Proxmox
privileges. Proxmox checks the actual request and may require storage or other
resource permissions as well.

| Action | Proxmox privileges |
| --- | --- |
| Inventory and measurements | `PVEAuditor`, including `VM.Audit` and `Sys.Audit` |
| Guest migration | `VM.Migrate` on `/vms/{vmid}` |
| Guest start, shutdown, stop, reset and reboot | `VM.PowerMgmt` on `/vms/{vmid}` |
| Hibernate | `VM.PowerMgmt`, `VM.Config.Disk`, and `Datastore.AllocateSpace` on the state storage |
| Free balloon | `Sys.Modify` on `/`, plus guest status access |

With privilege separation, both the user and token need the required privileges.
Free balloon is also limited to ProxHA administrators. ProxHA does not change
Proxmox permissions. See [Native Proxmox HA integration](#native-proxmox-ha-integration)
for HA-specific API access and optional SSH configuration.

### API endpoint failover

The configured node is the initial connection point. After a successful connection,
ProxHA learns the other node addresses and stores them. It prefers the last working
endpoint, tries known online nodes first and temporarily backs off unreachable ones.
The Connections view shows the active endpoint.

At least one successful connection is needed for discovery. Every alternate node
must be reachable from ProxHA on HTTPS port 8006 with the same token. ProxHA checks
cluster identity before using it for cluster operations.

With a trusted CA, alternate node certificates must pass chain, validity and
hostname checks. If verification is disabled without a trusted CA, ProxHA obtains
alternate certificate fingerprints through a known cluster endpoint and pins them
before sending the token to an alternate node.

Reads can retry on another endpoint. Writes retry only when the connection failed
before the request was sent. A lost response after submission is treated as an
uncertain outcome and reconciled before another overlapping operation is allowed.

## Placement rules

Migration domains describe which hosts can exchange guests. Resource groups
describe which guests or hosts a placement rule applies to. They serve different
purposes and are configured under **DRS > Rules**.

### Migration domains

Select **New migration domain**, enter a name and select compatible hosts.

Each host belongs to at most one domain, including disabled domains. An automatic
move needs two online hosts in the same enabled domain. Unassigned hosts are not
eligible. Remove a host from its current domain before adding it to another.

Verify migration compatibility in both directions before grouping hosts. Matching
CPU vendors alone is not sufficient. CPU features, guest settings, storage and
networking also matter. The boundary matrix shows domain permission; it does not
prove that an individual guest can migrate.

Domains apply to automatic VM migration and container restart migration. They do
not create Proxmox HA rules. Creating a domain does not enable balancing.

### Resource groups

Select **New resource group** and choose one of these types:

| Type | Members |
| --- | --- |
| Guest group | Any combination of VMs and containers |
| Host group | Hosts only |

A resource can belong to several groups. Stopped guests and offline hosts can
remain members. Groups use resource IDs, so migration or renaming does not remove
membership. Refresh the cluster inventory before selecting newly added resources.

A group's type cannot change after creation. A group referenced by a rule cannot
be deleted until those references are removed. Deleting a group does not delete
its guests or hosts.

### Create a rule

Select **New placement rule**, choose guest groups and, where needed, host groups.

| Rule | Purpose |
| --- | --- |
| Keep guests together | Place running group members on one host |
| Keep guests on separate hosts | Place running group members on different hosts |
| Keep guests on host groups | Limit placement to the selected hosts |
| Keep guests away from host groups | Exclude the selected hosts |

**Must** rules constrain placement. Together-rules form coordinated migration
groups, and separation rules prevent placing peers together when a legal
alternative is available. Overlapping together-rules combine their members.

**Should** rules are weighted preferences. Their weight affects the score without
making the preference a hard blocker. They do not trigger immediate repairs.

For example, put two DNS guests in one guest group and create a Must rule to keep
them on separate hosts. To prefer particular hosts without blocking alternatives,
create a host group and use a Should host-binding rule.

### Rule repair and balancing

Enabled automatic rule repair can move guests when a Must rule is violated,
including after group membership changes. This setting is separate from the
continuous balancing checkbox and is enabled by default. Review a rule before
enabling it if applying it would require a migration.

Background rule repair and VM-only balancing move VMs. They can move a VM to a
container that stays in place. The combined VM + LXC job can move both types and
coordinate mixed together-groups. Containers restart on the destination.

Together-groups are planned as a single placement decision, with destination
capacity reserved for the group. Their members can migrate in parallel within the
configured limit. Balancing cannot place separated guests together simply
to improve a host's load score.

Maintenance evacuation can override local placement rules. Host-binding rules
also allow fallback when no permitted host remains available within the domain.
Separation rules use the least conflicting placement when too few hosts are
available. Overrides are recorded. Native Proxmox HA rules are not relaxed by
these local exceptions.

Storage, locks, backup activity, host availability and Proxmox migration checks
still apply. DRS shows violations and candidate exclusions when a rule cannot be
satisfied. Changing a rule never makes local disks eligible for automatic migration.

### Guest stickiness

Select a guest to adjust its stickiness from 1 to 10. The default is 5.
Lower values make an otherwise eligible guest cheaper to move. Higher values
make it less attractive. A value of 10 blocks routine balancing and automatic
rule repair, with exceptions for verified maintenance or emergency evacuation.

Stickiness does not replace cooldowns, migration history or capacity checks.
See [Balancing](#balancing) for scoring details.

## Balancing

ProxHA evaluates guest placement using current load, recent measurements and
migration history. A proposed move must improve placement and pass the execution
checks before it is submitted to Proxmox.

### Modes

| Mode | Eligible guests | Execution |
| --- | --- | --- |
| Continuous | Running VMs | Repeated assessment while enabled |
| VM schedule | Running VMs | Independent calendar or Run now |
| VM + LXC schedule | Running VMs and containers | Independent calendar or Run now |

Both guest types contribute to host load and distribution targets in every mode.
VMs migrate live. Running containers shut down and restart on the destination.
Migration domains and placement rules apply to all automatic moves.

Continuous balancing checks every 30 seconds by default, with a 15-second option.
Aggressiveness ranges from 1 to 5 and defaults to 3. Higher values accept smaller
improvements and shorten observation and settling times.

The two calendars have independent enable flags, time zones and hourly, daily or
weekday schedules. Overlapping scheduled jobs run sequentially. Continuous
balancing waits until they finish, discards old observations and reassesses.
Each guest is attempted at most once in a run.

### Measurements

Collection runs every 15 seconds. Raw observations are retained for 24 hours;
minute summaries with means, peaks and coverage are retained for seven days.
Unavailable measurements remain unknown rather than being treated as zero.

CPU estimates use recent guest demand and host measurements adjusted to current
placement. Completed migrations therefore affect the source and destination
estimates without waiting for old averages to expire. Recent guest peaks inform
capacity reservations. RAM placement uses current measurements.

Guest and host PSI measure resource stalls. Optional physical disk and network
sources add destination guards. See
[Capacity and infrastructure](#capacity-and-infrastructure).

### Candidate scores

Candidates are ranked by urgency and then by their improvement score. The score
combines these terms:

1. Reduced CPU and memory pressure on the source and destination.
2. Improved distribution of demand, allocated RAM and memory commitments.
3. Capacity for returning ballooned RAM to VMs.
4. More even guest counts and workload categories within a migration domain.
5. Improved weighted Should placement preferences.
6. Migration cost, stickiness and applicable history penalties.

Distribution targets account for host capacity. Equal VM counts are not always
appropriate when hosts or guests differ in size. Guests are classified by
configured size and current demand so different workload types can be spread.

Migration cost estimates use RAM and observed traffic. They do not measure dirty
page rate, transfer duration or application downtime. Scores are relative
priorities, not probabilities or guarantees of application improvement.

Scheduled effort defaults to Balanced. Conservative accepts fewer routine moves;
Thorough reduces the migration-cost weight. The minimum scheduled improvement
score is configurable separately from continuous aggressiveness.

CPU and RAM overload thresholds default to 80% over five minutes. CPU relief
considers multiple busy guests and CPU stalls. Memory relief favors less active
guests when they can free capacity without moving the busiest workload.

### Migration history

Continuous balancing combines a decaying movement penalty, cooldowns, return
protection and a rolling cluster budget. The budget defaults to ten submissions
per hour. Failed attempts count toward it. Confirmed critical pressure can exceed
the budget while retaining placement and execution checks.

The movement penalty is capped at 100 and normally decays over 120 minutes.
Repeated moves lengthen the guest cooldown. Return protection covers six hours;
swap protection also checks recent moves by other guests between the same hosts.

A confirmed migration failure adds 200 penalty points, decreasing by one point
per minute. The failed guest is excluded from that run, and other candidates are
considered. Scheduled balancing ignores this failure penalty but retains its
cooldown, return and swap protections.

Ordinary Run now also retains history protection. Explicit Aggressive Run now
skips history penalties and cooldowns for that run. It still observes storage,
compatibility, running state, capacity, backup checks and stickiness 10.
Maintenance and rule repair have their own documented exceptions.

Completed automatic migrations are retained for seven days and shown per guest.
Manual guest moves and maintenance evacuation are excluded from that automatic
count. [Observed migration outcomes](#observed-migration-outcomes) compares
measurements around verified automatic moves.

### Execution checks

Before submission, ProxHA refreshes placement and checks quorum, host state,
domain membership, placement rules, guest locks, storage, network bridges and
destination capacity. Local disks, bind mounts and unsupported host devices
block automatic migration. HA guests require the optional
[native HA integration](#native-proxmox-ha-integration).

Active backups anywhere in the cluster prevent new balancing migrations.
Scheduled jobs are cancelled until their next slot. Continuous mode waits and
checks again. Submitted migrations finish rather than being interrupted.

Moves are normally serial. Together-groups reserve capacity for all incoming
members and may submit members in parallel within the configured limits.
Maintenance has a separate parallel limit, defaulting to three migrations.

Transient polling failures are retried. A successful task must also have its guest
placement verified. An uncertain outcome is reconciled before an overlapping
operation can proceed. Stop queue prevents new submissions and lets accepted
tasks finish. Queues continue when the browser closes.

### Compare settings without moving guests

Under **DRS > Settings**, use **Replay recorded data** or **Compare continuous
aggressiveness** to evaluate saved measurements. These tools require an
administrator and do not change live settings or submit migrations.

Replay uses up to 24 hours of raw observations. Profiles see the same recorded
workloads and placements. Proposed moves are not applied to later replay steps,
so the output is a diagnostic comparison rather than a simulation of completed
migrations.

The CLI reads the source database without modifying it:

```sh
python -m app.replay --connection 1 --hours 6 --step-minutes 1 --compare-levels 1,3,5
```

Compare candidate readiness, exclusions, coverage and reverse suggestions as
well as scores. Missing data or no suggestions does not prove perfect balance.

## Observed migration outcomes

Open a guest's **Summary > Automatic migrations > Recent automatic moves** and
select **Before / after**. The report compares CPU usage, used RAM, CPU/I/O/memory
PSI and guest ballooned RAM around a verified automatic migration. It also shows
the original priority score and elapsed time from submission to verified placement.
That duration is not measured guest downtime.

The baseline is the five complete minutes before submission. The comparison uses
five complete minutes starting at the next minute boundary after at least 60 seconds
of settling. For native HA cohorts, the baseline precedes the first member's
submission and the comparison follows the last member's verification or failure.
Host results describe the whole cohort; they must not be credited to each member
separately.

Each metric requires at least four minute summaries with three or four valid
observations each. Means are weighted by observation count. A guest must be running
on its expected host throughout each accepted summary; host summaries must be online.
Mixed-host minutes, stopped guests, unavailable values and incomplete windows remain
unknown. Coverage is displayed for both windows. PSI retains its documented source
and averaging limitations.

The report marks overlapping placement activity from ProxHA queues, observed host
changes and native migration tasks. Its own native cohort and associated task/event
records are excluded from that overlap check. A cohort member that is still
migrating keeps the report pending. A member that failed, ended with an unknown
outcome or was interrupted by a server restart makes the report confounded,
because its placement cannot be separated from this move. The report does not
prove causation: unrelated workload, backup, cache or configuration changes can
also change the measurements.

The migration score is a priority value, not a predicted CPU percentage reduction.
This report does not compare those incompatible units or automatically adjust
scoring weights. Transfer bytes, dirty-page rate and actual downtime are still
unavailable. Replay and observed comparisons support manual tuning.

Reports are calculated on demand from existing minute summaries and verified
migration records, without new Proxmox requests or another historical archive.
Both inputs retain the existing **seven-day maximum**. Manual, maintenance, failed,
unverified and expired moves are excluded. A report is scoped to its connection,
guest and operation ID.

API: `GET /api/connections/{id}/guests/{kind}/{vmid}/automatic-migrations/{operation_id}/outcome`.
The response status is `pending`, `measured`, `confounded` or `insufficient_data`.
Percentage changes are percentage points, not relative percentage improvements.

## Balloon recovery

ProxHA creates placement capacity for returning RAM to ballooned VMs. Proxmox's
native balloon controller performs the gradual return for DRS recovery. DRS does
not disable ballooning, raise guest minimums, change host thresholds, or send
competing balloon commands. A separate manual **Free balloon** action requests
the selected VM's full RAM allocation immediately.

### Manual Free balloon

Right-click one running QEMU VM in the inventory or cluster overview, choose
**Free balloon**, and submit the review. ProxHA reads live VM status and host
memory before sending `POST /nodes/{node}/qemu/{vmid}/monitor` with the fixed
command `balloon <maximum RAM in MiB>`. The maximum comes from live `maxmem`;
the browser cannot supply a monitor command or RAM target. The required Proxmox
privilege is `Sys.Modify` on `/`, in addition to inventory and status access.

This is a one-shot request to the balloon driver, with no migration or restart.
It does not change VM minimum RAM, maximum RAM, shares, or the host ballooning
target. Native Proxmox ballooning remains active and can reclaim RAM again.
Guest cooperation is required; an accepted command does not guarantee an
instantaneous return of every page.

The host must have space for `maxmem - balloon`, plus a reserve of the larger of
512 MiB and 2% of physical RAM. The native 80% ballooning target is not an admission
limit for this explicit action. Active migration queues or unresolved operations
block submission, and DRS waits while the release is being verified. Missing
metrics, offline hosts, templates, guest locks and paused/stopped VMs are rejected.
LXC is unsupported.

The request and its target are saved in SQLite before submission, using the
existing idempotent operation ID. Monitor errors, including error text returned
with HTTP 200, are recorded as failures. A lost response remains unknown and is
never automatically resent. **Recent tasks** and the background activity worker
read balloon allocation until full RAM is confirmed, or for at most 120 seconds.
They report incomplete recovery or an unverifiable outcome instead of claiming
success. Verification survives a backend restart; interrupted submissions require
review. Completed records use the existing seven-day history retention.

### Controls and execution

- **DRS header > Recover ballooned RAM** starts a dedicated, persistent migration queue.
  It only selects migrations that create additional recovery capacity, ranked by
  recovery benefit minus migration cost and negative contributions from other
  placement objectives. Guests move one at a time, followed by fresh measurements and the normal settling
  period. **Stop queue** stops further submissions; submitted migrations finish.
  When capacity already exists, the queue monitors native recovery for up to
  15 minutes from the start of the run. It reports incomplete recovery instead of
  declaring that RAM has returned when measurements still show a backlog.
- **DRS Settings > Prioritize restoring ballooned RAM** defaults to enabled.
  Recovery contributes to ordinary and continuous balancing scores. Disabling
  this priority does not disable native Proxmox ballooning or the explicit
  recovery action.
- Continuous execution still requires continuous balancing to be enabled.
  Existing compatibility groups, shared-storage requirements, cooldowns, pressure
  checks, maintenance exclusions, and stickiness rules remain in force.

The recovery panel displays the verified backlog, capacity currently available
for native recovery, and the remainder that needs capacity. These are estimates,
not confirmations that RAM has already returned. Subsequent balloon measurements
show the remaining backlog. Finishing a recovery migration queue does not mean
that the guest balloon drivers have finished returning RAM.

Overview shows measured allocation changes over five minutes for running VMs
whose host and configured RAM did not change. A positive value means RAM was
returned; a negative value means more was reclaimed. Capacity without progress
is flagged for checking pvestatd, guest balloon drivers and shares. Small residuals
around the native controller's 100 MiB adjustment step can remain visible.

### Measurement and placement

The collector reads `GET /nodes/{node}/config` once per minute and records
`ballooning-target` with its observation time. An absent property in a successful
response means the Proxmox default of 80%. A failed, invalid, or stale response
means an unknown threshold; it is never silently treated as 80%.

For a running QEMU VM, recovery requires a fresh balloon driver report, a positive
minimum below maximum RAM, nonzero shares, and no guest lock. Reclaimed RAM is
`maxmem - balloon`. Containers, disabled or fixed-size balloons, stale driver
statistics, and shares set to zero do not contribute verified recovery credit.
Their reported ballooned RAM remains visible as requiring verification.

For each host, native recovery capacity is:

```text
headroom = max(0, host_RAM * ballooning_target / 100 - host_RAM_used)
recoverable_now = min(verified_ballooned_RAM, headroom)
blocked_recovery = verified_ballooned_RAM - recoverable_now
```

Each simulated migration removes the VM's resident RAM from its source, adds
expected resident RAM at the destination, and budgets memory returning to the
remaining and incoming VMs up to each host's native threshold. A candidate earns
recovery credit for reducing the combined blocked recovery; capacity already
available without a migration is not credited again. Recovery points scale with
bytes released relative to host capacity, with a small bonus for allowing the
incoming VM to recover fully. Ordinary distribution and migration-history costs
still affect the final score.

Explicit recovery runs ignore positive benefits from other objectives, but retain
their negative contributions. Ordinary balancing always uses the combined score.
Recovery must pay for any worsening of resource risk or distribution as well as
migration cost. Continuous history penalties and
observation gates still apply. Resource-risk comparisons include the RAM that
native ballooning could already return without migration, so this existing
recovery is not incorrectly charged as a cost of the proposed move.

For routine placements with recovery priority enabled, the destination must reserve configured guest
RAM and its existing verified recovery backlog below its ballooning threshold,
with a two-percentage-point planning margin. Live preflight rereads the target
threshold and checks current memory usage before submitting a migration. Native
Proxmox may fill memory to the actual threshold rather than the planning margin.

Complete recovery is not possible when physical capacity, fixed guests, storage,
compatibility, or policy restrictions prevent a feasible placement. The UI reports
the remaining backlog and candidate exclusions instead of promising full recovery.

Configuration observations and recovery estimates use the existing telemetry and
evaluation storage: raw observations expire after one day and summaries and
evaluations after seven days. No additional unbounded history is created.

### Native behavior

Proxmox computes its memory goal from host usage and `ballooning-target`. Its
allocator returns memory in bounded steps, applies guest shares, and prioritizes
guests under memory pressure. A responding balloon driver is required.

## Native Proxmox HA integration

ProxHA targets Proxmox VE 9.2. Native HA is optional: an installed, idle HA manager
with no configured resources does not protect any guest. The DRS Overview shows
resource count, native rule count, fencing state and whether maintenance SSH is
configured. Missing API data is shown as unavailable. ProxHA never registers HA
resources or changes fencing, resource configuration or native rules.

### Balancing HA guests

Enable **DRS > Settings > Native HA guests** to include eligible HA guests.
They must be running, requested `started`, and have fresh, stable CRM state.
Legacy HA groups, unknown rules and unreadable policy block submission.

- Native node affinity restricts destinations. With failback enabled, only the
  highest-priority available hosts qualify, avoiding a move the CRM would reverse.
  With failback disabled, a strict rule permits any online listed host and a
  non-strict rule permits any online host, because the CRM keeps the guest where
  it is. Such a guest never needs rule repair.
- Negative node affinity excludes its listed hosts. A strict rule never permits
  those hosts; a non-strict rule permits fallback only when no other online,
  non-maintenance host remains. Unknown affinity values block HA eligibility.
- Negative resource affinity prevents placing peers together. Native constraints
  are not relaxed by ProxHA's availability or maintenance exceptions.
- Positive resource affinity forms a placement unit, including transitive peers.
  Every incoming member is checked for compatibility, storage, bridges, resource
  capacity and placement before one hand-off is submitted to Proxmox. Mixed VM
  and container groups require container restart migration to be enabled.
- Each incoming member consumes a concurrency slot. A native cohort must fit both
  the group and per-target limits; ProxHA cannot divide HA's co-migration command.
- The VM/container migrate API forwards HA-owned moves to the HA manager. The
  returned task confirms hand-off, not completed migration. Each member keeps its
  own operation and is verified against live guest placement and CRM state for
  up to 15 minutes. Restart reconciliation checks every member, including those
  sharing a hand-off task ID. An uncertain outcome prevents overlapping moves.
  The LRM migration task that follows a hand-off has its own task ID. ProxHA
  links it to the requesting run by guest, source host and start time, so the
  next wave of a group is not blocked by its own earlier move.
- HA manager and LRM timestamps count as fresh for 60 seconds and may be up to
  60 seconds ahead of the ProxHA server clock.

Proxmox retains control over native failover and CRS. If native automatic CRS
rebalancing is enabled alongside ProxHA, both can influence placement; choose one
routine balancer for HA guests. ProxHA does not change the native CRS setting.

### Host maintenance

With no HA resources, maintenance follows the ordinary ProxHA evacuation path.
When resources are configured, ProxHA requests native maintenance first. The
native manager chooses HA destinations and controls its own parallelism. ProxHA's
maintenance concurrency setting applies to the subsequent non-HA evacuation.

Before requesting native maintenance, ProxHA verifies inventory access, quorum,
backup inactivity, native rules, compatibility domains, shared storage and device
and network preconditions for possible native destinations. If native HA could
choose an incompatible host, constrain its native node-affinity rule first.
Stopped, unavailable or unsupported HA resources require manual review. Native
HA performs its own capacity placement; ProxHA does not reserve destinations for
native evacuation or claim that its normal capacity scoring controls the CRM.

ProxHA waits for the manager's maintenance request, a fresh LRM maintenance state,
no active HA services on the source and no resident HA guests. An LRM that runs no
HA services stays in `wait_for_agent_lock` and never enters the `maintenance`
state; a fresh idle LRM with the request recorded counts as acknowledged. ProxHA
requires two LRM timestamps at least ten seconds apart after evacuation. This
allows the LRM's maintenance loop to release its watchdog protection; the API does
not expose a separate watchdog-release acknowledgement. ProxHA then evacuates
non-HA guests. LRM migrations started by native maintenance on that host do not
block this evacuation. Native HA never moves HA resources requested `stopped`,
`disabled` or `ignored`, or those in the `error` state. If one remains on the
host, evacuation ends as blocked and lists it; migrate it or change its HA state
in Proxmox, then resume. Host power actions also require complete evacuation, no
outstanding tasks, quorum and a majority of nodes remaining online.

Maintenance ownership is persisted. ProxHA only disables native maintenance that
it requested itself, including requests for ProxHA-only maintenance hosts. For
maintenance enabled outside ProxHA, disable it in Proxmox before exiting here.
Exit waits for fresh native acknowledgement and keeps the local placement
exclusion until then. Background observation completes exit even if the browser
closes. **Native HA can migrate evacuated guests back to their
original host when maintenance ends.** Backup activity blocks that exit request.

Stopping the ProxHA queue does not cancel native evacuation already accepted by
the CRM. The host remains excluded. A server restart, shutdown or uncertain SSH
outcome never replays a maintenance command; shutdown stops a running SSH command
and records its outcome as unknown. ProxHA resolves an unknown command from the
manager's node request and the LRM mode:

- A recorded request means the command was applied; evacuation or exit continues.
- No recorded request after the HA manager has run for 2 minutes since the
  command means it was not applied. Resume evacuation to send it again, or exit
  maintenance; for an exit command, exit again to retry.
- A command the HA manager does not record within 2 minutes becomes a visible
  blocker instead of an endless wait. If the manager status is stale, the outcome
  stays unknown and ProxHA does not resend.

Maintenance that was enabled outside ProxHA and is later disabled in Proxmox no
longer blocks exit, even after a restart. If native maintenance is disabled
outside ProxHA during evacuation, the evacuation stops with a message; resume to
request native maintenance again. On a host without an evacuation run, ProxHA
shows such a disable as a blocker and does not enable native maintenance again
by itself.

### ProxHA maintenance hosts and native HA

Native HA does not know ProxHA exclusions. While HA resources exist, every host in
ProxHA maintenance is therefore also kept in native maintenance, so the CRM does not
choose it for recovery, failback or another host's maintenance. This includes hosts
without HA services and hosts that entered maintenance while the cluster had no HA
resources.

- The migration queue worker checks ProxHA maintenance hosts without an active
  evacuation run every 30 seconds; after HA resources first appear, a request
  follows within about 90 seconds. It requests native maintenance for online hosts
  that run no HA resources, once quorum, fresh HA manager status, armed HA, no
  active native migrations and complete inventory visibility are confirmed. An
  offline host is requested when it is online again; the CRM does not select
  offline hosts.
- These requests use the same acknowledgement, 2-minute timeout and unknown-outcome
  handling as other entries. A recorded request is never sent again, and a failed
  request is never retried automatically: the host is shown as blocked with the
  reason. Resume evacuation there to request it again, or exit maintenance.
- A host that runs HA resources is shown as blocked instead, because native
  maintenance would migrate them. Resume evacuation there to request it with the
  full preflight. Without SSH, the blocker gives the `ha-manager` command to run.
- When another host enters native maintenance and native HA could choose a
  ProxHA-only maintenance host, ProxHA requests native maintenance there first and
  the evacuation waits for its acknowledgement. If that request failed or needs
  manual action, entry stops with a blocker naming the host. A native node-affinity
  rule that keeps the guests away from that host removes the dependency.

### Optional SSH setup

The native maintenance command has no HTTP endpoint. Without SSH configuration,
the UI gives the supported command to run on a Proxmox host, then resume:

```sh
ha-manager crm-command node-maintenance enable HOSTNAME
ha-manager crm-command node-maintenance disable HOSTNAME
```

For one-click integration, set these server environment variables before startup:

| Variable | Meaning |
| --- | --- |
| `PROXHA_HA_SSH_KEY` | Absolute path to an existing private key |
| `PROXHA_HA_SSH_KNOWN_HOSTS` | Absolute path to a verified OpenSSH known_hosts file |
| `PROXHA_HA_SSH_USER` | SSH user with permission to run ha-manager; defaults to root |

SSH uses port 22 and the currently working Proxmox API endpoint's hostname. Prepare
verified host keys and access for every failover endpoint. Interactive passwords,
prompts and unknown host keys are rejected. Local SSH configuration is ignored;
no host-key verification bypass or arbitrary command template is supported.
Windows needs an OpenSSH client on PATH. The Docker image includes it; mount the
key and known_hosts read-only and make them readable by the app user (UID 10001).
Both can be mounted under `/config/ssh/` and selected with `ssh.key_file` and
`ssh.known_hosts_file` in `/config/settings.json`.
Keep keys out of Git and never paste their contents into the UI.

HA policy/status reads require `Sys.Audit`; migration requires `VM.Migrate` on
affected guests and the normal Proxmox storage/destination privileges. SSH access
is separate from the API token. ProxHA does not install keys or modify hosts.

## Capacity and infrastructure

### N+1 capacity

Choose **DRS > Overview > Check N+1 capacity**. The report simulates losing each
available host separately. It lists proposed destinations, guests left without
space, storage/placement restrictions, missing measurements and any availability
exceptions. It never starts migrations, registers HA resources or restarts guests.
The HA-managed count makes clear how many guests native HA could manage.

The simulation keeps surviving guests on their current hosts. It reserves:

- **Configured RAM** (default): at least each running guest's configured RAM and
  measured resident RAM. This avoids assuming ballooning will remain available.
- **Observed RAM + margin**: the larger of resident RAM and used RAM × 1.2. This
  is an estimate, not a guarantee for future workload peaks.
- Host memory overhead plus 10% RAM or 2 GiB headroom, whichever is larger.
- Current CPU demand with a 10% vCPU floor, adjusted by relative host CPU
  performance. Hosts retain 10% CPU headroom. CPU is a demand estimate rather
  than a guarantee that every configured vCPU can run simultaneously.

Only running guests are included. Offline and maintenance hosts are not targets.
The check uses enabled migration domains, shared-storage visibility, Must rules,
native HA constraints and weighted Should preferences. Must together-groups are
packed together; surviving guests are not rearranged to make space. Local disks
are unavailable after losing their host. Storage replication recovery is not
assumed. Stickiness and migration cooldowns are not failover capacity reservations.

A host failure is not maintenance, so Must rules are hard constraints: a guest is
never placed against a Must rule while a compliant host has room, and the
maintenance override is not used. Only the documented availability exceptions
apply: a host binding is relaxed when no permitted host remains in the
compatibility domain, and a separation rule uses the least conflicting host when
too few hosts remain. Native HA rules have no exceptions.

Scenario results:

- **Fits**: every affected guest has a placement that satisfies all rules.
- **Rule exceptions**: every guest has a placement, but only with a Must rule
  availability exception. The report lists the exceptions.
- **Insufficient**: a shortfall is proven. Either an exhaustive search found no
  complete placement, an upper bound shows that fewer guests can restart than are
  affected (for example a guest with no permitted target, or too little remaining
  RAM/CPU), or a surviving host already exceeds its reserved capacity before any
  failure.
- **Unknown**: required capacity or storage evidence is missing or stale.
- **Search limited**: the search budget ran out before a complete placement was
  found or ruled out; placement is not proven impossible.

The search first makes two ordered passes: largest guests first on the host with
the most free memory, then smallest guests first. Compliant hosts come before
hosts that need an exception, then Should preferences. If neither pass reaches the
upper bound on restartable guests, stronger bounds are computed. They can
prove a shortfall without any search:

- **Pooled and per-host bounds**: the RAM and CPU that remain on the hosts each
  guest fits on, filled with the smallest guests first.
- **Host sets**: guests that fit only on a set of hosts (because of migration
  domains, storage or Must rules) compete for that set alone, as in Hall's
  marriage condition.
- **Martello-Toth L2**: guests larger than half of every host they fit on cannot
  share a host, and similar size classes are counted with dual feasible
  functions, for RAM and CPU separately.
- **Subset sums**: a host can only be filled to a sum of guest sizes. When sizes
  share a unit, such as whole GiB of configured RAM, free capacity below that
  unit is unusable. Other sizes are rounded down to a fine grid first, which
  keeps the bound valid.

If the bounds do not decide the scenario, a branch-and-bound search looks for a
placement that restarts as many guests as the upper bound allows. An exhaustive
search that finds none proves the bound one guest lower, and the search repeats
with that target. Guests with the fewest permitted hosts go first, then the
largest in the tighter of RAM and CPU. Each guest tries the host with the most
room first. A short depth-first search runs first, then a limited-discrepancy
probe that revisits each earlier choice once, then the exhaustive search. The
search skips equivalent placements of interchangeable guests (same demand and
targets, no rules or preferences; a dimension that no longer constrains any
eligible host may differ) and of interchangeable hosts (same remaining room and
eligibility), and it remembers visited states. It also applies the subset-sum
bound at each state while that keeps pruning.

Each failed host has a search budget of 2 seconds, and a cap of 1,000,000 states
is a safety net. Scenarios with more than 200 placement units use only the
ordered passes and bounds. Each scenario reports its `search` usage: `states`,
`seconds`, `budget_seconds`, `budget_used` (share of the time budget) and
`stopped_by` (`time`, `states`, `units` or none). Should preferences order the
search but never decide the status. The report is a capacity estimate, not a
quorum, fencing, network reachability or disaster-recovery certification.

API: `GET /api/connections/{id}/capacity?memory_mode=configured|observed`.

### Automatic checks and warnings

Capacity monitoring is enabled by default and checks each connection every five
minutes. Configure it in **DRS > Settings > N+1 capacity monitoring**. Administrators
can disable it, select a 5/10/15-minute interval, or choose configured versus observed
RAM independently of the one-time assessment and balancing settings.

Results are available under **DRS > Overview > N+1 capacity**. The cluster warning
is off by default, including for existing connections. Administrators can enable
**Show cluster warning for insufficient or unverified N+1 capacity** in DRS Settings.
When enabled, the banner appears only for insufficient or unverified capacity;
successful checks and disabled monitoring do not show a banner. Background checks
and history continue independently of this display preference.

This is a failover reserve estimate, not a current load alarm. Normal RAM
overcommit can exceed the configured-RAM reservation model without indicating
current memory pressure. The display preference does not change capacity limits
or balancing decisions. Expand the assessed scenarios to see affected guests, placement
restrictions and remaining capacity. **Refresh monitor** requests a fresh background
check; viewers can read results and operators can request checks. Checks continue
with the browser closed and never submit migrations, reserve resources, enable
native HA or send external notifications.

- **Capacity available**: all assessed single-host failures have a placement
  without Must rule exceptions.
- **Failover reserve not met**: at least one scenario is insufficient or needs
  Must rule exceptions. The message names the affected failures. When a surviving
  host already exceeds its reserved capacity before any failure, it names that
  host instead of reporting every other host's loss. Any uncertain scenarios are
  also identified.
- **Capacity unverified**: missing data, failed API reads, a search budget that
  ran out without a complete placement or a proven shortfall, or an overdue
  result (see below).
- **Monitoring disabled**: no automatic checks are made.

The one-time report uses the same classification: a proven shortfall or a rule
exception makes it *at risk*, and a limited search alone makes it *unknown*.

The worker polls every 30 seconds and checks connections one after another. Each
check may take up to 45 seconds to read inventory, plus about 15 seconds and
4 seconds per host (twice the search budget) to calculate. A result is overdue
after the interval, plus one poll, plus this connection's check time, plus twice
the check time of every other
enabled connection. This allows for a running pass over the other connections and
for this connection's turn in the next pass. Host counts come from each
connection's last report.

A failed check replaces the current result with an unknown state and retries on
the next interval. CPU/RAM measurements must pass the same freshness checks as a
manual assessment. Results are point-in-time estimates: changes between checks
may affect capacity. The check does not test quorum, fencing or actual failover.

The server retains one current report per connection and seven days of compact
status changes. Identical summaries do not add entries; the UI shows the latest
30 changes. History does not store every hypothetical placement. Configuration
revisions and connection identity checks discard in-flight results after settings
or credentials change. Switching a connection URL clears its monitoring state.

API:

- `GET /api/connections/{id}/capacity-monitor`: cached status, report and history.
- `PUT /api/connections/{id}/capacity-monitor`: administrator-only settings with a revision.
- `POST /api/connections/{id}/capacity-monitor/refresh`: request an immediate check.

### Physical disk and network measurements

Proxmox guest I/O totals do not describe physical disk latency or queues. Optional
Prometheus node_exporter sources provide those measurements without SSH access or
Proxmox configuration changes. Install and expose node_exporter on each host
separately, then configure:

1. **DRS > Settings > Physical I/O and network sources**.
2. Select the Proxmox host and its HTTP(S) exporter URL, normally
   `http://host:9100/metrics`.
3. Select exact Linux physical device names, such as `nvme0n1,sda` and `eno1`.
   Select one layer of the device hierarchy: do not count a bond and its members,
   or device-mapper devices and their underlying disks, twice.
4. Enable collection. The first successful sample establishes counter baselines;
   rates become available after the next sample, normally 15 seconds later.

The exporter needs its `diskstats`, `netdev`, `netclass`, `uname` and `stat`
collectors. Host identity must match the Proxmox node name (the short hostname is
accepted). Explicit device selection is required. Disabled sources make no HTTP
requests. URLs cannot contain credentials or query strings. TLS certificates are
verified; redirects and environment proxy settings are not followed. Requests
have time/size limits and collection runs with bounded concurrency.

### Installing node_exporter on a Proxmox host

The repository includes `scripts/setup_node_exporter.py`.
Copy it to the host using your existing SSH access, or the Proxmox host console.
Use a specific management address reachable from the ProxHA server:

```sh
python3 setup_node_exporter.py --listen-address 192.0.2.10
sudo python3 setup_node_exporter.py --listen-address 192.0.2.10 --apply
```

The first command inspects only. The second installs Debian's
`prometheus-node-exporter` package without recommended packages, configures a
systemd drop-in and enables the exporter at boot. Package indexes must already be
usable; run `apt-get update` separately if installation reports stale indexes.
The script does not upgrade the system or change Proxmox, guests or firewall rules.
It temporarily masks only this service during installation to prevent the package
from starting with its default wildcard listener. It then enables only the five
collectors ProxHA needs, binds the requested address and checks the HTTP endpoint.

The result includes the exporter URL, physical disks and active physical network
interfaces. Review those devices, particularly for multipath or unusual storage
layouts, before selecting them in ProxHA. Repeating the same setup does not restart
an already configured exporter. An existing exporter not configured by this script
is left untouched; use its existing endpoint instead.

After installation, verify the URL from the ProxHA server, save the selected
devices in DRS Settings and enable collection. Verify two fresh samples before
enabling target protection. A locally successful exporter can still be unreachable
from ProxHA because of routing or firewall policy. Keep access to its HTTP endpoint
within the management network. Disabling collection in ProxHA does not stop the
host service; use `systemctl disable --now prometheus-node-exporter.service` to stop
it explicitly if it is no longer needed.

Metrics follow the Linux disk statistics and network statistics interfaces:

| Metric | Calculation |
| --- | --- |
| Physical disk latency | Change in read/write time divided by completed read/write operations, in milliseconds |
| Average disk queue depth | Change in weighted I/O time divided by elapsed wall time |
| Disk IOPS | Completed read/write operations per second |
| Disk busy time | Change in I/O busy time divided by elapsed time, capped at 100% |
| NIC capacity | Exporter link speed in bytes/s, displayed as Gbit/s |
| NIC utilization | Larger of RX and TX byte rates divided by link capacity; full-duplex directions are not added |
| Interface drops/errors | Counter deltas as a percentage of packet + drop + error deltas |

Interface drops/errors indicate local interface problems. They are **not
end-to-end packet loss**; remote path loss needs a separate active probe. Disk
latency is an interval average, not a latency percentile. Queue depth and busy
time need interpretation for parallel NVMe, RAID and shared storage. Changing a
source or selected devices changes what the historical aggregate represents.

Counter decreases, host reboot, source revision changes, missing fields and gaps
over 45 seconds produce unknown rates. Zero is retained only when reported or
derived from valid unchanged counters. Summary disk latency/queue values are
the worst selected disk; network utilization/drop/error summaries use the worst
selected interface. Rates and capacities are summed where appropriate.

The host detail and DRS Overview show current per-device values. The **24-hour
history** and **Seven-day history** buttons show minute means and peaks. Collection
runs every 15 seconds; raw observations expire after 24 hours and minute summaries
after **seven days**. Missing observations remain gaps. No measurements are retained
beyond the existing one-week history limit.

### Placement guards

**Protect migration targets** defaults on for a configured, enabled source.
Unconfigured or disabled sources do not change existing placement behavior.
With protection enabled, missing, incomplete or stale measurements block that
target. This includes the initial counter warm-up. Disabling protection keeps
collection and graphs active without blocking placement.

Defaults for sustained pressure:

- A selected disk has both latency ≥20 ms and average queue depth ≥2.
- A selected NIC uses ≥90% of its link capacity.
- A selected NIC reports ≥1% drops or errors, or its link is down.

Three pressured samples spanning at least 25 seconds are required, normally
30 seconds. All must be within 45 seconds of evaluation. The disk thresholds must
be met on the same disk. Thresholds are adjustable per host.

The planner excludes pressured targets before scoring and checks them again in
execution preflight. This applies to routine balancing, aggressive/scheduled
runs, coordinated groups, ProxHA evacuation and Start on best host. A different
eligible target can still be selected. ProxHA does not take ownership of native
HA's independent failover decisions. These metrics protect destination admission;
physical storage pressure alone does not trigger a migration, since moving a
guest that uses the same shared storage may not relieve it.

Source configuration is administrator-only and uses revisions to reject stale
edits. API routes:

- `GET /api/connections/{id}/infrastructure`
- `PUT /api/connections/{id}/infrastructure/{node}`
- `GET /api/connections/{id}/infrastructure/{node}/history?hours=24` (up to 168)

Replay includes stored infrastructure readings and does not use samples from
after the replay timestamp. Current source configuration is applied, just like
current placement rules and other replay settings.

## Container releases

The source repository is tommyo123/proxha on GitHub. Images are published to
`ghcr.io/tommyo123/proxha` for Linux amd64 and arm64.

### First publication

1. Push the source to `main` and enable GitHub Actions.
2. Allow the pinned actions used by `.github/workflows/container.yml` if the
   account restricts which actions may run.
3. Run **Build and publish container**, either through a push or **Run workflow**.
4. After the package is created, open its package settings and select public
   visibility if anonymous downloads are required. Repository and package
   visibility are separate settings.

Publication runs only for a public repository, on `main` or a tag beginning with
`v`. Pull requests build without publishing. No workflow changes repository
or package visibility.

The workflow uses `GITHUB_TOKEN` with `packages: write`; a personal token is not
required. For an existing package, grant the repository write access under the
package's **Manage Actions access** settings.

The image name is derived from the repository running the workflow. Forks use
their own namespace. Update the README, Compose image and Docker source label
when publishing under another name.

### What the workflow does

The workflow builds both architectures, publishes the combined image and
verifies that the published tags can be pulled from the registry.

### Image tags

| Source | Published tags |
| --- | --- |
| Push to `main` | `latest`, `main`, `sha-FULL_COMMIT_SHA` |
| Version tag, such as `v0.2.0` | `v0.2.0`, `sha-FULL_COMMIT_SHA` |

Version tags do not move `latest`. Commit tags allow deployments to stay on one
source revision. Containers update only when they are pulled and recreated.

### Source archives

Create an archive from a committed revision:

```sh
git archive --format=zip --output=proxha-source.zip HEAD
```

This includes tracked source files without Git history or untracked configuration,
credentials, dependencies and build output. The Docker build context separately
allows only runtime source, frontend assets and Python requirements.
