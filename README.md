# Spoke Valhalla nodes

Set up a fresh Ubuntu VPS for **Australia-only routing and matrices**, update its maps, and connect it through a Cloudflare Tunnel. No live or historical traffic feeds.

The routing configuration and Valhalla **3.8.3** image digest match Spoke's working configuration. This updates the map data without silently upgrading the routing engine. Auto and bus matrices retain the 2,500 location-pair / 400 km limits and existing algorithm settings.

## Fresh VPS

Use **Ubuntu 24.04 LTS, AMD64/x86_64, 2 vCPU, at least 2 GB RAM and 20 GiB free disk**. Ubuntu 22.04 is accepted but has not had the same hardware validation. A 60 GB disk is a practical starting point. More RAM gives the routing service more headroom under load.

Run on the VPS over SSH:

```bash
curl -fL https://raw.githubusercontent.com/Spoke-Technologies/valhalla-node/main/bootstrap.sh -o bootstrap.sh
sudo bash bootstrap.sh
```

Alternatively, clone this repo and run `sudo bash setup.sh`. The installer installs Docker if absent, Python and download tools, then starts the first Australia build as a **background systemd job**. It keeps running if SSH drops. The command returning means the job was started; wait for the build to pass:

```bash
sudo journalctl -fu valhalla-map-update
# Ctrl-C stops following the log; it does not stop the build.
sudo valhalla-node status
valhalla-node test
```

Detailed native build output is in `/opt/valhalla-node/releases/<release>/build.log`. The successful job ends with `Update passed and activated`. The clean installation, downloads, Australia build and activation took about **45 minutes** on the tested 2 GB / 2 vCPU VPS; allow longer on slower hosts.

On a fresh machine, there is no routing service until the first download, build and validation finish. During this stage, `status` reports `Current: none`, the map job state and that the first build is in progress. It exits with status 1 until routing is ready; this alone does not mean the build failed. Check the journal for the actual build result. Ctrl-C while following the journal only closes the log viewer. `build.log` appears after the map download finishes, when tile construction starts.

The journal reports the download starting, then received MiB, average speed and a percentage (when Geofabrik supplies a total size) every 30 seconds while data arrives. It reports checksum verification, tile construction, matrix validation and activation separately. While the tile build runs, it prints elapsed time and the latest build-log line every 30 seconds; a repeated line means that stage has not produced newer output, not that measurable progress has been confirmed. There is no overall build percentage because Valhalla does not supply one. Full build output remains in `build.log`. A stalled download can remain quiet until the network read times out (120 seconds).

An update to the CLI does not add feedback to a process that is already running. For an existing build, check the map download size with `sudo ls -lh /opt/valhalla-node/releases/<release>/custom_files/australia-latest.osm.pbf*`, and follow `sudo tail -f /opt/valhalla-node/releases/<release>/build.log` once that file exists. Let the current build finish rather than restarting bootstrap for these logging improvements.

This is a fresh-node installer, not an in-place migration tool for existing hand-managed Valhalla containers. It uses the names `spoke-valhalla`, `spoke-valhalla-build`, `spoke-valhalla-verify` and ports **8002 / 18002**. An occupied port stops the build before activation. Keep the old node and production hostname running until you have tested the new node from your application.

## Rename the VPS host

```bash
sudo valhalla-node rename-host valhalla-qld-2
```

Use a short hostname with 1–63 lowercase letters, digits or hyphens, starting and ending with a letter or digit. `localhost` and numeric-only names are rejected. The command applies the name immediately with `hostnamectl`, updates matching loopback aliases in `/etc/hosts` (adding an entry if needed), and saves a cloud-init override when cloud-init is installed so the name survives reboot. Reconnect SSH to see the new name in your shell prompt; no reboot is required.

This names the Ubuntu host. Cloudflare tunnel names, public DNS hostnames, and the VPS name in your provider's dashboard are managed separately. Existing installations need the updated CLI: in a checkout of this repository, run `git pull` and `sudo install -m 644 node.py /usr/local/lib/valhalla-node/node.py`. This refreshes the command without starting a map build.

## Connect Cloudflare

Once local routing passes, run interactively:

```bash
sudo valhalla-node cloudflare
```

The wizard first verifies local Valhalla status and routing matrices. It then walks you through creating a tunnel and a **new hostname for each node**, installs `cloudflared` from Cloudflare's signed package repository, connects it, and checks that the public endpoint has valid status fields and returns the same matrices as this node. Your domain must already be on Cloudflare and you need permission to create tunnels/routes.

Use a new hostname such as `valhalla2.spoketech.net`, with an HTTP origin of **`127.0.0.1:8002`**. The wizard leaves existing tunnels/hostnames alone. It does not change application endpoints or set up a load balancer.

The API listens on localhost, so you do **not** need inbound ports 8002, 80 or 443. Keep SSH access and allow outbound HTTPS for downloads and outbound TCP/UDP **7844** for Cloudflare. VPS/provider firewall rules are not changed.

The published hostname exposes the routing API unless you add Cloudflare Access separately. If your application uses Access service tokens, set those policies/headers before using it in production; the wizard's public probe expects an accessible `/status` endpoint. Tunnel tokens are entered with hidden input, stored only in root-readable files under `/etc/valhalla-node/`, and passed to cloudflared by file, never as command arguments or GitHub secrets. A separate `valhalla-cloudflared.service` manages this node's connector.

```bash
sudo systemctl status valhalla-cloudflared
sudo journalctl -u valhalla-cloudflared -n 50
curl https://YOUR-NEW-HOSTNAME/status
```

## Update Australia maps

```bash
sudo valhalla-node update
sudo journalctl -fu valhalla-map-update
```

Each update:

1. Downloads the latest [Geofabrik Australia extract](https://download.geofabrik.de/australia-oceania/australia.html) and verifies its published MD5 over HTTPS. The report also records SHA-256 and the resolved source URL.
2. Builds fresh admin/timezone databases and tiles in a separate release. It preserves the active routing configuration and default speed rules. Elevation, transit and traffic feeds are disabled.
3. Uses **one build thread / one CPU / 1 GiB Docker memory**, no extra container swap, and a **128 MiB build tile cache**. These settings completed the full Australia build on the 2 GB VPS alongside the serving container. Runtime cache settings are restored before testing.
4. Starts an isolated candidate on localhost:18002 with a 512 MiB memory cap and checks eight auto/bus matrices across Brisbane, Gold Coast, Sydney and Melbourne (**86 pairs**).
5. Switches the active release, restarts the service, and repeats the checks. Activation failure restores the previous release automatically. Map builds/checks failing earlier leave the current service running.

Updates are manual. Activation causes a brief routing interruption. Installation, updates and rollback share a process lock, so re-running setup during a build stops before installing packages or replacing files. The background unit is `valhalla-map-update`; if it fails, inspect its journal and the newest release's build/validation logs, then retry `update`. An interrupted build is rebuilt from scratch on the next update.

Activation saves and syncs recovery state and the previous release before switching maps. An interrupted activation is rolled back before the next service start, update or rollback command. This also restores the earlier rollback history. If the **first installation** is interrupted during activation, there is no old release to restore: recovery leaves routing stopped and you should run `sudo valhalla-node update` again. A rollback command that recovers an interrupted activation stops after restoring the last active maps; it does not roll back twice.

```bash
sudo valhalla-node status
valhalla-node test
sudo valhalla-node rollback
```

Rollback validates the previous release before switching. `/opt/valhalla-node/current` and `previous` point to the active and last active releases. All release directories are retained, including failed builds, so an operator can inspect them. They consume several GB each: after checking these two symlink targets, manually remove older, unused directories in `/opt/valhalla-node/releases/` when needed. The updater refuses to start with less than 20 GiB free.

The systemd routing service starts automatically after reboot. Its container uses rotating local logs, capped at three 10 MB files. `status` shows the active map timestamp; `source.json` and `verification.json` in each release record the downloaded source and checks. The latest available map can lag today's date according to Geofabrik's publishing schedule.

## Files and validation

Installed code: `/usr/local/lib/valhalla-node/`; command: `/usr/local/bin/valhalla-node`; data: `/opt/valhalla-node/`. Re-running the downloaded installer refreshes this code and requests a new map build. The updater copies the active `custom_files/valhalla.json`; local routing changes there are retained. Upgrading Valhalla itself requires a deliberate image/config review in this repo.

```bash
python3 -m unittest discover -s tests -v
for file in bootstrap.sh setup.sh valhalla-node run-service.sh cloudflare-setup.sh; do bash -n "$file"; done
shellcheck -e SC2034,SC1091 bootstrap.sh setup.sh valhalla-node run-service.sh cloudflare-setup.sh
```

Tests cover checksum rejection, matrix/status validation, installer locking, candidate failures, successful activation, rollback after failures, real process termination and recovery on the next service start. They also execute the public-check code with valid and invalid local/remote responses. Unit tests run on Linux; macOS skips the Linux-only `flock` shell test. ShellCheck and Ubuntu systemd unit validation are included in CI.

On 2 November 2026, the public bootstrap completed a clean application installation on Ubuntu 24.04 / 2 vCPU / 2 GB: Docker and old map data were removed first, while the OS and SSH were retained. It downloaded and built the full Australia extract, passed all eight auto/bus matrix cases, and activated with the original routing configuration. Two simultaneous 20×20 bus requests took about 2.1 seconds each; one 50×50 request took about 6.9 seconds; a 2,550-pair request was rejected at the configured limit. These are bounded functional checks, not a production throughput guarantee.

A second full update completed while the first release served routing requests. Two simultaneous 20×20 bus requests during tile construction took about 3.1 seconds each. The Cloudflare dashboard setup and all four wizard stages were tested on the VPS, including signed package installation, hidden token input, root-only token files and matching local/public matrices. Live testing found and fixed plain-terminal compatibility and Cloudflare's rejection of Python's default user agent; API requests now identify the CLI as `valhalla-node/1.0`.

Both CLI rollback directions passed using the two actual Australia releases. A test activation was terminated after changing the symlinks but before restarting routing, then the VPS was rebooted. Boot recovery restored the last active release and its previous-release pointer, cleared the pending journal, and passed all eight local/public matrix cases. Both services started automatically. This was a graceful OS reboot after process termination, not a hard power-loss test. Sampled probes observed a 503 during the map activation restart; builds do not provide uninterrupted activation. SSH reconnects were delayed after reboot while public routing was already healthy; the cause of those access delays remains unresolved.

The existing engine has a reproduced matrix limitation: a self-trip at `lat=-27.535, lon=153.09` returns 213 seconds / 1.837 km instead of zero. The unchanged old server and an isolated 3.9.1 comparison both reproduce it. The CLI preserves the original engine/configuration and does not rewrite results. The eight fixed smoke cases do not trigger this location-specific issue; test your application's actual locations before cutover.

Map data: © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), distributed by Geofabrik under the ODbL. Preserve the required attribution in the application displaying/using this data. [Valhalla](https://github.com/valhalla/valhalla) and [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/) retain their own licensing/terms.
