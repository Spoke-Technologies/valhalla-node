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

Detailed native build output is in `/opt/valhalla-node/releases/<release>/build.log`. The successful job ends with `Update passed and activated`. A complete Australia build took about **40 minutes** on the tested 2 GB / 2 vCPU VPS; allow longer on slower hosts.

This is a fresh-node installer, not an in-place migration tool for existing hand-managed Valhalla containers. It uses the names `spoke-valhalla`, `spoke-valhalla-build`, `spoke-valhalla-verify` and ports **8002 / 18002**. An occupied port stops the build before activation. Keep the old node and production hostname running until you have tested the new node from your application.

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

Tests cover checksum rejection, matrix/status validation, installer locking, candidate failures, successful activation, rollback after failures, real process termination and recovery on the next service start. They also execute the public-check code with valid and invalid local/remote responses. Unit tests run on Linux; macOS skips the Linux-only `flock` shell test. ShellCheck and Ubuntu systemd unit validation are included in CI. Cloudflare's signed package installation and token-file option were checked in a disposable Ubuntu 24.04 container. The build parameters and configuration were verified by a full Australia build and activation on the existing test VPS. The reusable installer still needs its first complete run on a fresh VPS; the Cloudflare wizard needs a human dashboard run. No throughput capacity is promised by these smoke checks.

Map data: © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), distributed by Geofabrik under the ODbL. Preserve the required attribution in the application displaying/using this data. [Valhalla](https://github.com/valhalla/valhalla) and [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/) retain their own licensing/terms.
