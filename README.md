# ProxHA

ProxHA is a web interface for managing and balancing Proxmox clusters. It shows
hosts, VMs and containers, tracks resource usage, and can migrate guests manually
or automatically. Connections, users, rules and history are stored in SQLite.

ProxHA targets Proxmox VE 9.x and is tested against 9.2. Automatic balancing needs
current CPU, memory and pressure measurements. Proxmox VE 8 is not supported for
automatic balancing. ProxHA does not replace native Proxmox HA.

## Start with Docker

Use Docker with support for Linux containers:

```sh
docker run -d --name proxha --restart unless-stopped --init --stop-timeout 60 -p 8080:8080 -v proxha-config:/config ghcr.io/tommyo123/proxha:latest
```

The image is available after a successful
[container publication](docs/reference.md#container-releases).
If no image has been published yet, use [Build from source](#build-from-source).

Open **https://SERVER-IP:8080**, or **https://127.0.0.1:8080** on the same machine.
The first start creates a self-signed certificate. Your browser will ask you to
trust it.

Sign in with **admin / password**. Password changes are optional. Change your
password from the top bar and add users under **Connections > Users and audit
log**. Any non-empty password is accepted and stored as an Argon2id hash.
Administrators manage settings and users, operators run guest operations, and
viewers have read access.

Docker creates the `proxha-config` volume automatically. Keep it when updating
or recreating the container. One instance can manage several clusters. Each
cluster should have only one active ProxHA controller.

### Docker Compose

Use this command instead of `docker run` if you prefer Compose:

```sh
docker compose up -d
```

Use `compose.yaml` from this repository. Compose manages its own persistent
volume. When switching from `docker run`, reuse or transfer your existing
configuration rather than starting with an empty volume.

### Build from source

```sh
git clone https://github.com/tommyo123/proxha.git
cd proxha
docker build -t proxha:local .
```

Use `proxha:local` instead of the GHCR image in the run examples. For a local
Compose build, use both files:

```sh
docker compose -f compose.yaml -f compose.build.yaml up --build -d
```

## Choose ports and protocols

In `-p HOST_PORT:CONTAINER_PORT`, the left side is the port on your machine.
The right side is the port inside the container.

| Mode | Environment option | Container ports |
| --- | --- | --- |
| HTTPS, the default | `PROXHA_PROTOCOL=https` | 8080 |
| HTTP | `PROXHA_PROTOCOL=http` | 8081 |
| Both | `PROXHA_PROTOCOL=both` | HTTPS 8080 and HTTP 8081 |

Choose one example below. If `proxha` already exists, follow
[Update or change configuration](#update-or-change-configuration) first.

HTTPS on port 8443:

```sh
docker run -d --name proxha --restart unless-stopped --init --stop-timeout 60 -p 8443:8080 -v proxha-config:/config ghcr.io/tommyo123/proxha:latest
```

HTTP on port 8080:

```sh
docker run -d --name proxha --restart unless-stopped --init --stop-timeout 60 -p 8080:8081 -e PROXHA_PROTOCOL=http -v proxha-config:/config ghcr.io/tommyo123/proxha:latest
```

HTTPS on port 8443 and HTTP on port 8080:

```sh
docker run -d --name proxha --restart unless-stopped --init --stop-timeout 60 -p 8443:8080 -p 8080:8081 -e PROXHA_PROTOCOL=both -v proxha-config:/config ghcr.io/tommyo123/proxha:latest
```

Both listeners serve the application. HTTP does not redirect to HTTPS and sends
credentials and traffic without encryption. For local access only, use a mapping
such as `-p 127.0.0.1:8443:8080`.

For Compose, set `PROXHA_PROTOCOL`, `PROXHA_HTTPS_PORT` and `PROXHA_HTTP_PORT` in
your shell or a `.env` file beside `compose.yaml` before starting it.

## Use your own certificate

Prepare two PEM files:

1. `fullchain.pem`: your server certificate followed by its intermediate certificates.
2. `privkey.pem`: the matching private key, without a password.

For example, place them in `/srv/proxha-certs` on the Docker host:

```sh
docker run -d --name proxha --restart unless-stopped --init --stop-timeout 60 -p 8443:8080 -v proxha-config:/config --mount type=bind,source=/srv/proxha-certs,target=/certs,readonly -e PROXHA_TLS_CERT_FILE=/certs/fullchain.pem -e PROXHA_TLS_KEY_FILE=/certs/privkey.pem ghcr.io/tommyo123/proxha:latest
```

The same example in PowerShell, using `C:\proxha-certs`:

```powershell
docker run -d --name proxha --restart unless-stopped --init --stop-timeout 60 -p 8443:8080 -v proxha-config:/config --mount "type=bind,source=C:\proxha-certs,target=/certs,readonly" -e PROXHA_TLS_CERT_FILE=/certs/fullchain.pem -e PROXHA_TLS_KEY_FILE=/certs/privkey.pem ghcr.io/tommyo123/proxha:latest
```

The environment variables use paths inside the container. Both files must be
readable by its user, UID 10001. Open ProxHA using the hostname covered by the
certificate. An internal CA must also be trusted by the browser.

After renewal, replace the files and run `docker restart proxha`. ProxHA does not
modify these files or renew certificates automatically. Missing, expired or
mismatched files prevent HTTPS startup.

Without these options, ProxHA keeps its generated certificate under `/config/tls`.
Add `-e PROXHA_PUBLIC_HOST=proxha.example.com` on the first start to include your
hostname. This option does not regenerate an existing certificate.

## Configuration options

Pass options with `docker run -e NAME=value`. The examples above do not require
editing files in `/config`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROXHA_PROTOCOL` | `https` | Select `https`, `http` or `both` |
| `PROXHA_TLS_CERT_FILE` | Unset | PEM certificate chain path |
| `PROXHA_TLS_KEY_FILE` | Unset | Matching PEM private key path |
| `PROXHA_PUBLIC_HOST` | Unset | Hostname or IP for a newly generated certificate |
| `PROXHA_ALLOWED_HOSTS` | Any hostname in a new container | Comma-separated hostnames or IPs, without schemes or ports |
| `PROXHA_CONFIG_DIR` | `/config` in Docker | Persistent data directory |
| `PROXHA_LISTEN_HOST` | `0.0.0.0` in Docker | Application listening address |
| `PROXHA_LISTEN_PORT` | `8080` | Internal HTTPS port |
| `PROXHA_HTTP_LISTEN_PORT` | `8081` | Internal HTTP port |

Normally, use `-p` to change published ports and leave internal ports unchanged.
Environment options override `settings.json` on every start without modifying
that file. Removing an option restores the saved value. Command-line options
take precedence over environment options.

## Connect a cluster

1. Create a dedicated Proxmox user and API token under **Datacenter > Permissions**.
2. Grant `PVEAuditor` on `/` with propagation. With privilege separation enabled,
   grant the required access to both the user and token.
3. In ProxHA, select **Connect cluster**. Enter a name, a node address such as
   `https://prox01.example.com:8006`, the token ID and its secret.
4. Keep **Verify TLS certificate** enabled for publicly trusted certificates.
   For a Proxmox cluster CA, use **Paste CA certificate** or **Trust cluster CA**
   and confirm its fingerprint.
5. Save. ProxHA tests the connection before storing it.

`PVEAuditor` provides monitoring access. Migrations also need `VM.Migrate`, and
guest power actions need `VM.PowerMgmt`. See
[Cluster connections](docs/reference.md#cluster-connections) in the reference for
CA setup, the permission table and endpoint failover behavior.

After connecting, ProxHA discovers the other nodes and can use another API
endpoint when the configured node goes offline. Those endpoints must be reachable
from the ProxHA server.

## Configure balancing

Under **DRS > Rules**, create a migration domain with hosts that can exchange
guests. A host belongs to one domain, and automatic moves stay within an enabled
domain. Creating a domain does not start balancing.

Under **DRS > Settings**, choose the modes you want:

| Mode | Guests it can move | When it runs |
| --- | --- | --- |
| Continuous balancing | VMs | Repeated assessments while enabled |
| VM balancing | VMs | Its own schedule or Run now |
| VM + LXC balancing | VMs and containers together | Its own schedule or Run now |

All three modes are off by default. Each schedule has its own interval, daily
time or selected weekdays and times. Time entry uses 24-hour format. Scheduled
jobs run after one another. Continuous balancing waits for them to finish, then
reassesses using fresh measurements.

VMs migrate live. Running containers use restart migration and have downtime.
All modes count both VM and container load. Automatic balancing excludes stopped
guests and guests with local disks. Compatibility, placement rules, storage and
available capacity still apply. Backup activity cancels scheduled balancing and
pauses continuous moves.

Use **Evaluate** to inspect candidates and scores. **Run now** reassesses after
each move. **Stop queue** prevents new submissions and lets accepted migrations
finish. Together-groups can move members in parallel. Continuous aggressiveness
and scheduled balancing effort are configured separately.

See [Placement rules](docs/reference.md#placement-rules) in the reference for
domains, groups, Must and Should rules, and stickiness. The
[balancing reference](docs/reference.md#balancing) explains scoring and protection
against repeated moves.

## Daily use

Select a datacenter, host or guest to see its status and resource usage. The
resource tree supports multiple selection and drag-and-drop migration.
Right-click a guest for power and migration actions.

Right-click a host and select **Enter maintenance mode** to exclude it from
placement and evacuate its guests. Maintenance allows three parallel migrations
by default, adjustable in DRS Settings. Container evacuation uses restart
migration. **Exit maintenance mode** makes the host available again. Local disks
and other migration blockers still need to be resolved.

Native Proxmox HA guests require the optional
[HA integration](docs/reference.md#native-proxmox-ha-integration). ProxHA
maintenance and native Proxmox HA maintenance are separate operations.

The task list shows progress, failures and pending work. DRS exclusions explain
why a guest cannot move. **Free balloon** requests full configured RAM for a VM
when host capacity allows; see
[Balloon recovery](docs/reference.md#balloon-recovery) in the reference.

## Data and backups

Docker stores application data in `/config`. A local installation uses `config/`
in the project directory.

To use a host directory instead of a Docker volume, replace
`-v proxha-config:/config` with `--mount type=bind,source=/srv/proxha,target=/config`.
Create the host directory first. On Linux, it must be writable by UID/GID
`10001:10001`. Keep `/config` as the container path.

| File or directory | Contents |
| --- | --- |
| `proxha.sqlite3` | Users, connections, rules, settings, tasks and measurements |
| `secret.key` | Encryption key for saved Proxmox tokens |
| `settings.json` | Server defaults |
| `tls/` | Generated HTTPS certificate and private key |

Measurements are collected every 15 seconds. Raw samples are kept for 24 hours,
and minute summaries with averages and peaks for seven days. Automatic migration
history is kept for seven days. The user audit log is kept for 90 days. Accounts,
connections and rules remain until you remove them.

Let active migrations finish before stopping ProxHA. For a consistent backup,
stop the application and copy the whole configuration directory:

```sh
docker stop --time 60 proxha
docker cp proxha:/config ./proxha-backup
docker start proxha
```

Use a new backup directory each time. Keep the database and `secret.key` together.
Back up externally mounted certificates separately. To restore, stop ProxHA,
restore the configuration into its volume or directory, and start it again.
The container user must have write access to the restored files.

## Update or change configuration

Back up the configuration, then pull the image and recreate the container:

```sh
docker pull ghcr.io/tommyo123/proxha:latest
docker stop --time 60 proxha
docker rm proxha
```

Repeat your chosen `docker run` command with the same `proxha-config` volume,
ports, environment options and certificate mounts. Removing the container does
not remove the named volume.

With Compose, run `docker compose pull` followed by `docker compose up -d`.
Keep the same Compose project and volume. For a source build, use
`git pull --ff-only` and rebuild instead of pulling an image.

## Run without Docker

Use Python 3.14.

Windows, from the project directory:

```powershell
.\start.ps1
```

Linux or macOS:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m app.server
```

Open **https://127.0.0.1:8080**. Local startup listens on loopback by default.
For LAN access, set `PROXHA_LISTEN_HOST=0.0.0.0` and
`PROXHA_PUBLIC_HOST=your-server-name` before starting. The same protocol and
certificate variables work outside Docker.

For an older installation using `data/`, stop its server and run
`python -m app.server --migrate-from data` inside the virtual environment.
This copies the database and encryption key to `config/` without overwriting an
existing destination. Use normal startup commands afterwards.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| The page does not open | Run `docker logs proxha`. Check the URL scheme, enabled listener and port mapping. |
| Invalid host name | Add the name to `PROXHA_ALLOWED_HOSTS` or set `PROXHA_PUBLIC_HOST`. |
| Custom certificate fails | Check both paths, read permissions, validity and whether the key matches. |
| A guest is not proposed for migration | Read its DRS exclusion reason. Check domains, local storage, rules, fresh metrics, backups and capacity. |
| A migration failed | Check the task result. Confirmed failures are recorded and other candidates can proceed. Uncertain outcomes need reconciliation before overlapping moves. |
| N+1 reports insufficient capacity | This estimates capacity after a host failure. It does not by itself mean current load is too high. |

## Further reading

The [reference document](docs/reference.md) describes cluster connections,
placement rules, balancing, balloon recovery, native Proxmox HA integration,
N+1 capacity, physical disk and network measurements, observed migration
outcomes and container releases.

The frontend uses plain JavaScript with no build step. The interface icons are
Heroicons, included under their MIT license; see `static/icons/LICENSE`.

## License

Released under the [MIT License](LICENSE).
