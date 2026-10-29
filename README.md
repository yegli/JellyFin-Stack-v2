# Jellyfin stack

Your own Netflix, running on a box at home. Someone searches for a film,
clicks Request, and a while later it shows up ready to play on the TV.

Six programs do the work. You never have to think about most of them:

| | | |
|---|---|---|
| **Seerr** | the request page | this is the one you share with the household |
| **Jellyfin** | the player | apps on TV, phone, browser |
| Radarr / Sonarr | find films and series | automatic |
| Prowlarr | keeps the list of sources | you set it once |
| qBittorrent | does the downloading | automatic |
| FlareSolverr | gets past Cloudflare checks | automatic |

Normally you would set each one up by hand and connect it to the other five.
That is five setup wizards and a long afternoon. Here one command does all of
it, and you can run that command again any time without breaking anything.

## What you need

A machine that stays on. An Intel NUC or any mini PC is ideal, a Raspberry Pi
is not (no hardware video support). Linux is the expected OS. macOS works for
trying it out.

Install Docker and make:

```bash
sudo apt install docker.io docker-compose-v2 make        # Debian / Ubuntu
```

Add yourself to the `docker` group so you do not need `sudo` for everything,
then log out and back in:

```bash
sudo usermod -aG docker $USER
```

## Get it running

```bash
git clone <this repo>
cd Jellyfin_Stack
make .env          # writes your settings file and invents all the passwords
```

Open `.env` in an editor. Three lines are worth your attention, the rest have
sensible defaults:

```ini
TZ=Europe/Zurich
DATA_ROOT=./data                          # where films and series are stored
JELLYFIN_PUBLISHED_URL=http://nuc.local:8096
```

`DATA_ROOT` should point at your big disk once you have one, for example
`/srv/media`. The folder gets created for you. One rule: downloads and the
finished library have to sit on the **same disk**, which they do if you only
change this one line. Split them across two disks and every film quietly takes
up twice the space.

`JELLYFIN_PUBLISHED_URL` is the address other devices use to reach the server.
If you do not know the machine's name yet, use its IP: `http://192.168.1.50:8096`.

Then:

```bash
make
```

That starts everything and configures it, which takes a few minutes. When it
finishes it prints the two addresses you need. Your logins are written to
`CREDENTIALS.md` in this folder.

> **One thing to avoid:** do not open Jellyfin in a browser until `make` has
> finished. Jellyfin creates an admin account for whoever shows up first and
> then locks its setup down for good. If you did it anyway, run
> `make nuke && make` to start over. Nothing already downloaded is lost.

## First evening

1. Open Seerr (port 5055). Sign in with the Jellyfin username and password
   from `CREDENTIALS.md`.
2. Search for a film. Click **Request**. Watch it appear in Jellyfin maybe
   twenty minutes later, with cover art and a description.
3. Install the Jellyfin app on the TV. It should find the server by itself. If
   it asks, give it the address from `JELLYFIN_PUBLISHED_URL`.

To let the family in, make them a Jellyfin account each (Jellyfin, Dashboard,
Users) then in Seerr go to **Settings, Users, Import Jellyfin Users**. They
sign in to both with the same password.

Under Settings, Users, Seerr decides what a new person may do. **Request** means
their choices wait for your approval. **Auto-Approve** means they do not. Start
with Request until you trust the disk space.

## Everyday commands

Run these from this folder.

| | |
|---|---|
| `make` | start everything, and fix anything that drifted |
| `make check` | test that all six programs are still talking to each other |
| `make down` | stop |
| `make logs` | watch what everything is doing |
| `make backup` | save your settings and watch history |
| `make pull` | update to newer versions of the six programs |
| `make nuke` | throw away all settings and start fresh (keeps your films) |

`make` is safe to re-run. It only touches what is missing or wrong, so it is
also the fix for "something looks off".

## Where everything lives

```
DATA_ROOT/
  torrents/        downloads land here while they run
    movies/
    tv/
  media/
    movies/        finished, renamed, this is what Jellyfin shows
    tv/
```

Films move from `torrents/` to `media/` without being copied, so a 10 GB film
takes 10 GB and not 20. That is why both folders have to be on one disk.

Everything else is in this project folder: `config/` holds each program's own
database, `.env` holds your settings and passwords. Back up `.env`. Lose it and
every program needs new keys.

## The ports

| | |
|---|---|
| 5055 | **Seerr**, the request page, share this one |
| 8096 | Jellyfin, the player |
| 9696 | Prowlarr, where download sources live |
| 8989 | Sonarr, series |
| 7878 | Radarr, films |
| 8080 | qBittorrent, downloads in progress |

You will use the first two. The others are there when something needs a look.

## Download sources

Sonarr and Radarr cannot find anything on their own. They ask Prowlarr, and
Prowlarr needs a list of sites to search. That list is `indexers.toml` in this
folder, and `make` applies it for you. Seven public sites are set up already,
which is enough to start.

Each entry has a `priority`. When two sites have the same film, the **lower**
number wins. Entries you comment out are left alone in Prowlarr rather than
deleted, so nothing you added by hand disappears.

Three things that normally go wrong are handled:

- Some sites hide behind a Cloudflare check and return **zero results** without
  a solver, which looks exactly like "nothing available". FlareSolverr is part
  of the stack and the right sites are pointed at it automatically.
- Public sites change domains constantly. If the main address is dead, `make`
  tries the alternatives and tells you which one worked. Re-running never
  pushes a working address back to a broken one.
- Prowlarr rate-limits itself when several sites are added at once, and the
  ones that hit the limit never reach Sonarr and Radarr. Adds are paced.

A site that only has films shows up in Radarr and not in Sonarr. That is
correct and not a missing connection.

If a site reports `TLS certificate was rejected`, your router or internet
provider is blocking it rather than the site being down. Turn on the VPN below.

## Use a VPN

Public download sites hand your home IP address to everyone else downloading
the same file. If that matters to you, and in most places it should, get a
WireGuard config from a VPN provider and put it in `.env`:

```ini
VPN_ENABLED=true
VPN_SERVICE_PROVIDER=mullvad       # or protonvpn, nordvpn, airvpn, ivpn, ...
VPN_TYPE=wireguard
WIREGUARD_PRIVATE_KEY=...
WIREGUARD_ADDRESSES=10.x.x.x/32
VPN_COUNTRIES=Switzerland
```

Then `make`. Only qBittorrent goes through the tunnel, on purpose: it is the
one program that talks to other people. Jellyfin has to stay reachable on your
own network, so tunnelling it would only break the TV.

If the tunnel ever drops, downloads stop rather than leaking. Check it is
really carrying traffic:

```bash
docker compose exec gluetun wget -qO- https://ipinfo.io/ip    # not your own IP
```

Downloads being slow is usually about inbound connections, not the VPN itself.
If your provider supports port forwarding (ProtonVPN, PIA, PrivateVPN, Perfect
Privacy), set `VPN_PORT_FORWARDING=on`. Without it you only reach people who
can reach you, which can halve a swarm.

## Hardware video

A NUC's built-in graphics can transcode video far more cheaply than its CPU.
If the machine has it, this is picked up automatically. Check:

```bash
ls -l /dev/dri/renderD128      # has to exist
```

If it does, `make` passes it through and switches Jellyfin over. Confirm after
a run by playing something that needs converting and looking for "Transcoding
(hardware)" on Jellyfin's dashboard.

`LIBVA_DRIVER_NAME` in `.env` is `iHD` for Intel chips from 2014 onward and
`i965` for older ones. If video fails with a driver error, try the other.

Do not `chmod` the device to fix a permission error. It resets on every reboot.

## Names instead of port numbers

Optional. Turn it on and every program gets a hostname on port 80:

```ini
PROXY_ENABLED=false     # set to true
PROXY_DOMAIN=home
```

| | |
|---|---|
| `home`, `jellyfin.home` | Jellyfin |
| `requests.home`, `seerr.home` | Seerr |
| `sonarr.home`, `radarr.home`, `prowlarr.home`, `qbit.home` | the rest |

The numbered ports keep working, so this only adds a second way in. If it ever
gets in the way, use `:8096` and ignore it.

**This does not make the names resolve.** Something still has to tell your
devices that `jellyfin.home` means this machine. Three options:

1. **A hosts file entry.** One minute, but on every device separately, and
   impossible on most TVs.
   ```bash
   sudo sh -c 'echo "192.168.1.50  home jellyfin.home requests.home" >> /etc/hosts'
   ```
2. **Your router.** Five minutes, works for every device, nothing extra to run.
   Most routers take static DNS entries or a wildcard. Point `*.home` at the
   machine. This is the right answer for most homes.
3. **A real domain.** Point `*.home.example.com` at the machine's private IP in
   your public DNS. Works everywhere with no per-device setup, and because the
   names are real you can get proper certificates without exposing anything to
   the internet. Set `PROXY_DOMAIN=home.example.com` and drop the `http://`
   prefixes in the `Caddyfile`.

`.local` names will not work here. macOS and iOS resolve `nuc.local` but have
no concept of `jellyfin.nuc.local`.

Running the VPN and the proxy together needs one extra line, because
qBittorrent has no network of its own inside the tunnel:

```ini
QBITTORRENT_HOST=gluetun
```

## Who can open what

Sonarr, Radarr and Prowlarr ask for no password on your own network. That is
what makes the whole thing work without clicking through logins, and it means
**anyone on your wifi can open them**. On a home network that is a reasonable
trade. On a shared or office network it is not. To require a login:

```ini
ARR_AUTH_METHOD=Forms
ARR_AUTH_REQUIRED=Enabled
```

Then `make up`, and create a user in each of the three once. That last step has
no API and cannot be automated.

Those three will also warn that "Allowed Hosts is not configured". The setting
ships empty deliberately. Empty costs you the warning, while a wrong value
locks your own browser out with `Invalid Host`. To silence it, list the names
you actually use:

```ini
ARR_ALLOWED_HOSTS=localhost,127.0.0.1,nuc.local,192.168.1.50
```

Do not forward any of these ports to the internet. If you want access from
outside the house, use Tailscale or WireGuard, and expose only Jellyfin and
Seerr.

## Backup, and moving to another machine

```bash
make backup
```

Writes a single archive holding `.env`, every program's database, and Jellyfin's
users and watch history. The stack stops for a few seconds, because copying a
database mid-write gives you a broken copy.

Your films are **not** in there. They are plain files in `DATA_ROOT`, so copy
them with `rsync` when you want to.

To move to a new machine, install Docker there, copy this folder across, and:

```bash
make restore BACKUP=backup/stack-20261026-1830.tar.gz
$EDITOR .env        # check DATA_ROOT and JELLYFIN_PUBLISHED_URL
make
```

## When something is wrong

Start here:

```bash
make check
```

It tests every connection between the six programs and names whatever is
broken. Most problems are fixed by running `make` again.

### A download sits at 0%

```bash
make shell-qbt
```

This prints what every download is doing and what each tracker says back. A
state of `metaDL` with size 0 means it has not reached a single other person
yet, so the problem is your network and not the film or the site.

| What you see | What it means |
|---|---|
| `timed out` on every `udp://` tracker | UDP is blocked. Common on office, school and guest wifi, and it kills the backup peer discovery too, so there is nothing left |
| `Forbidden` or 403 on an `http://` tracker | something is intercepting the request. A real tracker never replies with a web page |
| `Host not found` | that tracker is dead. Normal. Only a problem if they all say it |
| `connection_status: firewalled` | no inbound port. Slow but not fatal, see VPN port forwarding |

If UDP is blocked, no choice of download source will help. Turn on the VPN, or
move the machine to a normal home connection.

### Downloads are enormous and take all night

Already handled by default, but worth knowing why. Jellyfin's quality presets
allow Blu-ray "Remux" files, which score highest and are 25 to 40 GB for one
film. `ARR_EXCLUDE_QUALITIES` in `.env` turns those off, which gets you the
same film at 2 to 12 GB.

The opposite problem is handled too. Every quality level ships with no minimum
size, so a 1.7 GB file claiming to be 1080p wins on being small and looks like
mud. `ARR_MIN_MB_PER_MIN` sets a floor. Series get their own numbers
(`TV_MIN_MB_PER_MIN`) because the limit applies per episode, and film numbers
would wave through a 70 GB season.

Change any of them and run `make provision`.

### Nothing can be found at all

Open Prowlarr (port 9696) and look at the indexer list. A red site is blocked
or has moved. See the download sources section above.

## Things that will surprise you

- **Do not open Jellyfin before the first `make` finishes.** It takes over the
  admin account. Recovery is `make nuke && make`.
- **qBittorrent has no default password.** It prints a random one on each boot.
  `make` reads it out of the log once and sets the real one for you.
- **`QBITTORRENT_PORT` has to be the same number inside and outside.**
  qBittorrent checks it and answers `401` before looking at your password, so
  changing one side locks you out with a misleading error.
- **Versions are pinned** in `.env`, so a reboot never surprises you with an
  upgrade. `make pull` upgrades when you decide to.
- **Jellyfin's settings live in a Docker volume**, not a folder you can see. Its
  database gets corrupted on some filesystems. `make backup` includes it.
- **Seerr, not Jellyseerr.** Jellyseerr and Overseerr stopped in February 2026.
  Seerr is the continuation, and old Jellyseerr logins no longer work against
  current Jellyfin.

## What the files are

| | |
|---|---|
| `compose.yaml` | the six programs |
| `compose.hwaccel.yaml` | hardware video, added when the machine has it |
| `compose.vpn.yaml` | the VPN tunnel, added when `VPN_ENABLED=true` |
| `compose.proxy.yaml`, `Caddyfile` | hostnames, added when `PROXY_ENABLED=true` |
| `.env` | every setting and every password. Not in git. Back it up |
| `indexers.toml` | your download sources |
| `provision.py` | the five setup wizards, as a script |
| `Makefile` | the commands above |
| `CREDENTIALS.md` | your logins, written for you. Not in git |

The three extra compose files are optional add-ons. Docker reads them on top of
the main one, and the `Makefile` includes each only when it applies, so you get
the same project whether or not the machine has a GPU or you want a VPN.
