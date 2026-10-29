# Zero-click Jellyfin stack. `make` is the whole interface.
#
#   make            bring the stack up and configure every app end to end
#   make provision  re-run configuration only (idempotent, safe any time)
#   make check      verify every app is wired to every other one
#   make shell-qbt  why a download is stuck: torrent + tracker status
#   make down       stop
#   make logs       follow everything
#   make backup     tar .env + app databases + Jellyfin history (NOT media)
#   make restore BACKUP=<file>   put a backup back, e.g. on the NUC
#   make nuke       stop and delete all app state (NOT your media)

SHELL := /bin/bash
# Intel Quick Sync only exists where /dev/dri does. Layer it on when it is there.
HWACCEL_FILE := $(shell test -e /dev/dri && echo "-f compose.hwaccel.yaml")
# Opt-in, from .env, so the VPN is a one-word change and not a different command.
VPN_FILE := $(shell grep -sqE '^VPN_ENABLED=true' .env && echo "-f compose.vpn.yaml")
PROXY_FILE := $(shell grep -sqE '^PROXY_ENABLED=true' .env && echo "-f compose.proxy.yaml")
COMPOSE := docker compose -f compose.yaml $(VPN_FILE) $(PROXY_FILE) $(HWACCEL_FILE)
SECRETS := PROWLARR_API_KEY SONARR_API_KEY RADARR_API_KEY SEERR_API_KEY
# Volumes worth carrying: Jellyfin's users, watch history and library ids, plus
# Caddy's certificate store. jellyfin-cache is regenerated artwork - skip it.
KEEP_VOLUMES := jellyfin-config caddy-data caddy-config
BACKUP_DIR ?= backup
PASSWORDS := JELLYFIN_ADMIN_PASS QBITTORRENT_PASS

.PHONY: up provision dirs check shell-qbt backup restore chown-data down restart logs ps pull nuke
.DEFAULT_GOAL := up

up: .env dirs
	$(COMPOSE) up -d
	@$(MAKE) --no-print-directory provision

provision: .env
	@python3 -c 'import sys; sys.exit(sys.version_info < (3,11))' \
	  || { echo "provision.py needs python3 >= 3.11 (tomllib)"; exit 1; }
	@COMPOSE="$(COMPOSE)" python3 provision.py

# Asserts the stack is really wired up, not just that provisioning said OK.
check: .env
	@COMPOSE="$(COMPOSE)" python3 provision.py --check

# Why a torrent is stuck: per-torrent state plus every tracker's message.
shell-qbt: .env
	@COMPOSE="$(COMPOSE)" python3 provision.py --torrents

# Everything that cannot be regenerated: .env secrets, the app databases, and
# Jellyfin's users/history. NOT the media - that is 21 GB of plain files sitting
# in DATA_ROOT, which you rsync separately (see README).
#
# The stack is stopped for the duration: these are live SQLite databases and a
# copy taken mid-write restores corrupt.
backup: .env
	@set -a; . ./.env; set +a; \
	 mkdir -p $(BACKUP_DIR); t=$$(mktemp -d); out=$(BACKUP_DIR)/stack-$$(date +%Y%m%d-%H%M).tar.gz; \
	 echo "stopping stack for a consistent copy..."; $(COMPOSE) stop >/dev/null 2>&1; \
	 cp .env "$$t/env"; cp -R config "$$t/config"; \
	 for v in $(KEEP_VOLUMES); do \
	   docker run --rm -v jellyfin-stack_$$v:/v -v "$$t":/out alpine \
	     tar czf /out/vol-$$v.tar.gz -C /v . 2>/dev/null || true; done; \
	 tar czf "$$out" -C "$$t" .; rm -rf "$$t"; \
	 $(COMPOSE) start >/dev/null 2>&1; \
	 echo "wrote $$out ($$(du -h "$$out" | cut -f1))"; \
	 echo "media NOT included - rsync $$DATA_ROOT separately"

# make restore BACKUP=backup/stack-20261026-1830.tar.gz
# Overwrites .env and config/ on purpose: that is the point of a migration.
restore:
	@test -n "$(BACKUP)" || { echo 'usage: make restore BACKUP=backup/stack-....tar.gz'; exit 1; }
	@test -f "$(BACKUP)" || { echo "no such file: $(BACKUP)"; exit 1; }
	@read -rp "Overwrite .env and config/ from $(BACKUP)? [y/N] " a; [[ $$a == y ]]
	$(COMPOSE) down 2>/dev/null || true
	@t=$$(mktemp -d); tar xzf "$(BACKUP)" -C "$$t"; \
	 cp "$$t/env" .env; rm -rf config; cp -R "$$t/config" config; \
	 for v in $(KEEP_VOLUMES); do \
	   [ -f "$$t/vol-$$v.tar.gz" ] || continue; \
	   docker volume create jellyfin-stack_$$v >/dev/null; \
	   docker run --rm -v jellyfin-stack_$$v:/v -v "$$t":/in alpine \
	     sh -c "rm -rf /v/* /v/..?* 2>/dev/null; tar xzf /in/vol-$$v.tar.gz -C /v"; done; \
	 rm -rf "$$t"
	@$(MAKE) --no-print-directory chown-data
	@echo "restored. Review DATA_ROOT and JELLYFIN_PUBLISHED_URL in .env, then: make"

# PUID/PGID differ between machines (502:20 on macOS, usually 1000:1000 on Linux),
# so transferred files end up owned by a uid that does not exist here.
chown-data: .env
	@set -a; . ./.env; set +a; \
	 if [ "$$(id -u)" = "0" ] || command -v sudo >/dev/null; then \
	   echo "chown $$PUID:$$PGID on config/ and $$DATA_ROOT"; \
	   sudo chown -R "$$PUID:$$PGID" config "$$DATA_ROOT" 2>/dev/null \
	     || chown -R "$$PUID:$$PGID" config "$$DATA_ROOT" 2>/dev/null || true; \
	 fi

down: ; $(COMPOSE) down
restart: ; $(COMPOSE) restart
logs: ; $(COMPOSE) logs -f --tail=50
ps: ; $(COMPOSE) ps
pull: ; $(COMPOSE) pull

# Wipes app state so the next `make` provisions from scratch. Media is untouched.
nuke: .env
	@read -rp "Delete all app config (Jellyfin users, Sonarr/Radarr DBs)? [y/N] " a; [[ $$a == y ]]
	$(COMPOSE) down -v
	rm -rf config CREDENTIALS.md
	@echo "state gone. .env (and its secrets) kept - delete it too for a truly cold start."

dirs: .env
	@set -a; source .env; set +a; \
	 mkdir -p "$$DATA_ROOT"/{media,torrents}/{movies,tv} config

# One-time: real values for everything .env.example leaves blank.
#
# Deliberately has NO prerequisite. With `.env: .env.example`, editing the
# example makes .env "stale" and the next `make up` silently overwrites it with
# freshly generated secrets - which orphans the Jellyfin admin account and the
# qBittorrent password that are already deployed. An existing .env is never
# touched; `make nuke` plus deleting it is the way to start over on purpose.
.env:
	@cp .env.example .env
	@sed -i.bak -e "s|^PUID=.*|PUID=$$(id -u)|" -e "s|^PGID=.*|PGID=$$(id -g)|" .env
	@gid=$$(getent group render 2>/dev/null | cut -d: -f3); \
	 [[ -n $$gid ]] && sed -i.bak "s|^RENDER_GID=.*|RENDER_GID=$$gid|" .env || true
	@for k in $(SECRETS); do \
	   sed -i.bak "s|^$$k=.*|$$k=$$(openssl rand -hex 16)|" .env; done
	@for k in $(PASSWORDS); do \
	   sed -i.bak "s|^$$k=.*|$$k=$$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | cut -c1-20)|" .env; done
	@rm -f .env.bak
	@chmod 600 .env
	@echo "wrote .env (secrets generated). Review DATA_ROOT and JELLYFIN_PUBLISHED_URL."
