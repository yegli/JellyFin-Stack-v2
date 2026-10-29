#!/usr/bin/env python3
"""Configure the whole stack over its APIs. Idempotent: safe to re-run, and
safe to run against a wiped config/ after `make up`.

Everything here is normally done by clicking through five setup wizards.
Secrets come from .env (generated once by the Makefile) and are summarised in
CREDENTIALS.md at the end.
"""
import json, os, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from http.cookiejar import CookieJar

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)


def log(*a):
    print(*a, flush=True)


# ---------- .env ----------
def load_env(path='.env'):
    if not os.path.exists(path):
        sys.exit('no .env - run `make` instead of calling this directly')
    e = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            v = v.strip()
            # Values containing spaces must be quoted, because the Makefile
            # `source`s this file and the shell would otherwise treat the rest of
            # the line as a command. Strip the quotes back off here.
            if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
                v = v[1:-1]
            e[k.strip()] = v
    return e


E = load_env()
# Set by the Makefile so the hwaccel overlay is not lost when we restart a service.
COMPOSE = (os.environ.get('COMPOSE') or 'docker compose -f compose.yaml').split()
HOST = 'http://localhost'
JF = f'{HOST}:{E["JELLYFIN_PORT"]}'
QB = f'{HOST}:{E["QBITTORRENT_PORT"]}'
PROWLARR = f'{HOST}:{E["PROWLARR_PORT"]}'
SONARR = f'{HOST}:{E["SONARR_PORT"]}'
RADARR = f'{HOST}:{E["RADARR_PORT"]}'
SEERR = f'{HOST}:{E["SEERR_PORT"]}'

VPN = (E.get('VPN_ENABLED') or '').lower() == 'true'
PROXY = (E.get('PROXY_ENABLED') or '').lower() == 'true'
QB_HOST = 'gluetun' if VPN else 'qbittorrent'       # shared netns when tunnelled
QB_PORT = int(E['QBITTORRENT_PORT'])

ok = lambda st: st in (200, 201, 202, 204)


# ---------- http ----------
def http(url, data=None, headers=None, method=None, timeout=120, form=False, raw=False, opener=None):
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
    h = {'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json'}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h,
                                 method=method or ('POST' if data is not None else 'GET'))
    try:
        send = opener.open if opener else urllib.request.urlopen
        with send(req, timeout=timeout) as r:
            t = r.read().decode()
            if raw:
                return r.status, t
            return r.status, (json.loads(t) if t.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:                                   # noqa: BLE001 - network, any error means "not up"
        return 0, repr(e)


def wait(url, want=(200, 301, 302, 307, 401, 403), label='', tries=80):
    for _ in range(tries):
        if http(url, raw=True)[0] in want:
            return True
        time.sleep(3)
    log(f'  !! {label or url} never came up')
    return False


def arr(base, key):
    def call(path, data=None, method=None, timeout=120):
        return http(base + path, data, {'X-Api-Key': key}, method, timeout)
    return call


def setfields(schema, values):
    for f in schema.get('fields', []):
        if f['name'] in values:
            f['value'] = values[f['name']]
    return schema


def compose(*args):
    subprocess.run([*COMPOSE, *args], capture_output=True)


# ================= qBittorrent =================
# qBittorrent 4.6.1+ has no default password: it prints a random one per boot.
# So the only way in on a cold start is to scrape it from the container log,
# then set the persistent one from .env.
def qbittorrent():
    log('\n== qBittorrent ==')
    wait(QB + '/', label='qbittorrent')
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar()))

    def login(pw):
        st, _ = http(QB + '/api/v2/auth/login', {'username': 'admin', 'password': pw},
                     {'Referer': QB}, form=True, raw=True, opener=opener)
        if not ok(st):
            return False
        return ok(http(QB + '/api/v2/app/version', raw=True, opener=opener)[0])

    def temp_login():
        logs = subprocess.run([*COMPOSE, 'logs', '--no-log-prefix', 'qbittorrent'],
                              capture_output=True, text=True)
        m = re.findall(r'temporary password is provided for this session: (\S+)',
                       logs.stdout + logs.stderr)
        return bool(m) and login(m[-1].strip('.'))

    pw = E['QBITTORRENT_PASS']
    if login(pw):
        log('  password already set')
    elif not (temp_login() or rekey(login, temp_login)):
        log('  !! could not get in; set the password by hand and re-run')
        return
    prefs = {'web_ui_password': pw, 'save_path': '/data/torrents',
             'temp_path_enabled': False, 'max_ratio_enabled': True, 'max_ratio': 2.0,
             'max_ratio_act': 0, 'queueing_enabled': True,
             'max_active_downloads': 3, 'max_active_torrents': 5,
             'listen_port': int(E['BT_PORT']),
             # Without Automatic Torrent Management a category's save path is
             # ignored and everything lands in the default directory, so the
             # radarr/sonarr categories below would be decorative. Applies to
             # newly added torrents; anything already downloading is left alone.
             'auto_tmm_enabled': True}
    if VPN:
        # Gluetun's port-forward hook POSTs the new port from inside the shared
        # network namespace, where it is 127.0.0.1. Only gluetun itself is
        # "localhost" there - the LAN is not - so this is not a hole in the WebUI.
        prefs['bypass_local_auth'] = True
    http(QB + '/api/v2/app/setPreferences', {'json': json.dumps(prefs)},
         {'Referer': QB}, form=True, raw=True, opener=opener)
    log(f'  prefs: save path /data/torrents, seed ratio 2.0, BT port {E["BT_PORT"]}'
        + (', localhost auth bypassed for gluetun' if VPN else ''))
    # Categories keep Sonarr's and Radarr's downloads apart on disk.
    for cat, path in (('radarr', '/data/torrents/movies'), ('sonarr', '/data/torrents/tv')):
        http(QB + '/api/v2/torrents/createCategory', {'category': cat, 'savePath': path},
             {'Referer': QB}, form=True, raw=True, opener=opener)
    log('  categories radarr -> /data/torrents/movies, sonarr -> /data/torrents/tv')


def rekey(login, temp_login):
    """A password is persisted and we do not know it (someone set it by hand, or
    .env was regenerated). Drop the stored hash so qBittorrent falls back to a
    fresh temporary one."""
    conf = 'config/qbittorrent/qBittorrent/qBittorrent.conf'
    stored = os.path.exists(conf) and 'Password_PBKDF2' in open(conf).read()
    if not stored:
        # No hash to clear, so the rejection is not about the password. By far the
        # likeliest cause is qBittorrent refusing the Host header.
        log('  !! qBittorrent rejected a password it never stored. Check that'
            ' QBITTORRENT_PORT is both the published AND the internal port'
            ' (a mismatch makes qBittorrent answer 401 on every request).')
        return False
    log('  stored password unknown -> clearing the saved hash to re-key')
    compose('stop', 'qbittorrent')
    if os.path.exists(conf):
        keep = [l for l in open(conf) if 'Password_PBKDF2' not in l and 'Password_ha1' not in l]
        open(conf, 'w').writelines(keep)
    compose('start', 'qbittorrent')
    wait(QB + '/', label='qbittorrent')
    time.sleep(8)
    return temp_login()


# ================= Sonarr / Radarr =================
# Identical shape, three differences: the API path, the root folder, and what
# the qBittorrent client calls its category field. Probe the last one from the
# schema rather than hardcoding movieCategory/tvCategory.
def arr_app(label, base, key, root, category, profile_want, size_env):
    log(f'\n== {label} ==')
    wait(base + '/api/v3/system/status', want=(200, 401), label=label.lower())
    call = arr(base, key)
    st, cur = call('/api/v3/rootfolder')
    if st == 401:
        log('  !! API key rejected. The app kept an older key from a previous run;'
            '  `make nuke` or match ' + label.upper() + '_API_KEY to config/' + label.lower() + '/config.xml')
        return None
    if not any(f['path'] == root for f in (cur or [])):
        log(f'  root folder {root}:', 'OK' if ok(call('/api/v3/rootfolder', {'path': root})[0]) else 'FAIL')
    else:
        log(f'  root folder {root}: present')

    st, cur = call('/api/v3/downloadclient')
    if not any(c.get('implementation') == 'QBittorrent' for c in (cur or [])):
        st, sch = call('/api/v3/downloadclient/schema')
        s = next(x for x in sch if x['implementation'] == 'QBittorrent')
        s.update(name='qBittorrent', enable=True)
        vals = {'host': QB_HOST, 'port': QB_PORT, 'useSsl': False,
                'username': 'admin', 'password': E['QBITTORRENT_PASS']}
        cat_field = next((f['name'] for f in s['fields']
                          if f['name'].endswith('Category') and 'Imported' not in f['name']), None)
        if cat_field:
            vals[cat_field] = category
        setfields(s, vals)
        st, r = call('/api/v3/downloadclient', s)
        log('  qBittorrent client:', 'OK' if ok(st) else f'FAIL {st} {str(r)[:120]}')
    else:
        log('  qBittorrent client: present')

    st, profs = call('/api/v3/qualityprofile')
    prof = next((p for p in (profs or []) if p['name'] == profile_want), (profs or [{}])[0])
    log(f'  quality profile: {prof.get("name")}')
    exclude_qualities(call, prof)
    quality_sizes(call, size_env)
    return {'profileId': prof.get('id'), 'profileName': prof.get('name')}


def quality_sizes(call, prefix):
    """Put a floor and a ceiling under release size, in MB per minute.

    Every quality ships with minSize=0, so a 1.7 GB 'x265' encode of a 106
    minute film - 2.2 Mb/s, where decent 1080p is 8-15 - satisfies Bluray-1080p
    and wins on size. It plays, and it looks like mud: blocking, banding and
    smeared motion, worst of all on animation. Excluding Remux without also
    setting a floor just trades 40 GB of waste for an unwatchable file.

    Movies and series need DIFFERENT windows, hence the prefix. The limit is
    per-minute and evaluated per episode, so a movie window applied to TV is
    wrong at both ends: it waves through a 3.3 GB/episode Blu-ray season pack
    (72 GB for one season of a 43 minute show) while rejecting the ~700 MB x265
    WEB-DL episode that is actually what you want.

    1 MB/min = 0.133 Mb/s."""
    lo = float(E.get(f'{prefix}_MIN_MB_PER_MIN') or 0)
    hi = float(E.get(f'{prefix}_MAX_MB_PER_MIN') or 0)
    if not lo and not hi:
        return
    st, defs = call('/api/v3/qualitydefinition')
    changed = []
    for d in (defs or []):
        if '1080p' not in d['quality']['name']:
            continue
        before = (d.get('minSize'), d.get('maxSize'), d.get('preferredSize'))
        if lo:
            d['minSize'] = lo
        if hi:
            d['maxSize'] = hi
        # preferredSize is the target size and must sit inside [min, max], or the
        # save is rejected wholesale. Pin it to the ceiling: the best quality the
        # budget allows, rather than an arbitrary leftover default.
        if d.get('preferredSize') is not None and hi:
            d['preferredSize'] = min(max(d['preferredSize'], lo or 0), hi)
        if (d.get('minSize'), d.get('maxSize'), d.get('preferredSize')) != before:
            changed.append(d)
    if not changed:
        log('  release size limits: already applied')
        return
    st, r = call('/api/v3/qualitydefinition/update', changed, 'PUT')
    if not ok(st):
        log(f'  release size limits: FAIL {st} {str(r)[:140]}')
        return
    # This endpoint answers 202 Accepted and commits in the background, so a
    # read straight after can still show the old values. Confirm rather than
    # report success on an acknowledgement.
    want = {d['id']: (d['minSize'], d['maxSize']) for d in changed}
    for _ in range(10):
        time.sleep(1)
        st, now = call('/api/v3/qualitydefinition')
        if all((d['minSize'], d['maxSize']) == want[d['id']]
               for d in (now or []) if d['id'] in want):
            log(f'  release size limits {lo}-{hi} MB/min'
                f' (~{lo*8/60:.1f}-{hi*8/60:.1f} Mb/s) on {len(changed)} qualities: OK')
            return
    log(f'  release size limits: accepted but not applied after 10s - re-run')


def exclude_qualities(call, prof):
    """Turn off the qualities named in ARR_EXCLUDE_QUALITIES. Remux is on by
    default in HD-1080p and scores highest, so a 1080p film arrives as a 25-40 GB
    Blu-ray remux - hours of downloading for something a NUC then has to
    transcode anyway. Without Remux the same film is a 2-12 GB encode."""
    excl = {q.strip() for q in (E.get('ARR_EXCLUDE_QUALITIES') or '').split(',') if q.strip()}
    if not excl or not prof.get('items'):
        return
    changed = False
    for it in prof['items']:
        for leaf in ([it] if it.get('quality') else it.get('items', [])):
            if leaf.get('quality', {}).get('name') in excl and leaf.get('allowed'):
                leaf['allowed'] = False
                changed = True
        # A group whose every member is now off must not stay enabled.
        if it.get('items') and it.get('allowed') and not any(x.get('allowed') for x in it['items']):
            it['allowed'] = False
            changed = True
    if not changed:
        log(f'  excluded qualities: already applied')
        return
    # The cutoff must point at something still allowed, or the save is rejected.
    allowed = [it for it in prof['items'] if it.get('allowed')]
    ids = {(it.get('id') or it['quality']['id']) for it in allowed}
    if allowed and prof.get('cutoff') not in ids:
        prof['cutoff'] = allowed[-1].get('id') or allowed[-1]['quality']['id']
    st, r = call(f'/api/v3/qualityprofile/{prof["id"]}', prof, 'PUT')
    log(f'  excluded {", ".join(sorted(excl))}:', 'OK' if ok(st) else f'FAIL {st} {str(r)[:100]}')


# ================= Prowlarr =================
def prowlarr():
    log('\n== Prowlarr ==')
    wait(PROWLARR + '/api/v1/system/status', want=(200, 401), label='prowlarr')
    call = arr(PROWLARR, E['PROWLARR_API_KEY'])
    st, cur = call('/api/v1/downloadclient')
    if st == 401:
        log('  !! API key rejected; see the note above for Sonarr/Radarr')
        return
    if not cur:
        st, sch = call('/api/v1/downloadclient/schema')
        s = next(x for x in sch if x['implementation'] == 'QBittorrent')
        s.update(name='qBittorrent', enable=True)
        setfields(s, {'host': QB_HOST, 'port': QB_PORT, 'useSsl': False,
                      'username': 'admin', 'password': E['QBITTORRENT_PASS'],
                      'category': 'prowlarr'})
        log('  qBittorrent client:', 'OK' if ok(call('/api/v1/downloadclient', s)[0]) else 'FAIL')
    else:
        log('  qBittorrent client: present')

    # Prowlarr pushes indexers into Sonarr and Radarr itself, so indexers are
    # configured in exactly one place.
    st, apps = call('/api/v1/applications')
    have = {a['name'] for a in (apps or [])}
    st, sch = call('/api/v1/applications/schema')
    for name, impl, url, akey in (('Sonarr', 'Sonarr', 'http://sonarr:8989', E['SONARR_API_KEY']),
                                  ('Radarr', 'Radarr', 'http://radarr:7878', E['RADARR_API_KEY'])):
        if name in have:
            log(f'  {name} app: present')
            continue
        s = json.loads(json.dumps(next(x for x in sch if x['implementation'] == impl)))
        s.update(name=name, syncLevel='fullSync')
        setfields(s, {'prowlarrUrl': 'http://prowlarr:9696', 'baseUrl': url, 'apiKey': akey})
        st, r = call('/api/v1/applications', s)
        log(f'  {name} app (fullSync):', 'OK' if ok(st) else f'FAIL {st} {str(r)[:120]}')
    apply_indexers(call, flaresolverr(call))


def flaresolverr(call):
    """Register FlareSolverr and return the tag id that activates it. Prowlarr
    applies a proxy only to indexers carrying its tag, so the tag is the handle
    apply_indexers needs."""
    st, tags = call('/api/v1/tag')
    tag = next((t for t in (tags or []) if t['label'] == 'flaresolverr'), None)
    if not tag:
        st, tag = call('/api/v1/tag', {'label': 'flaresolverr'})
        if not ok(st):
            log(f'  !! could not create flaresolverr tag: {st}')
            return None
    st, proxies = call('/api/v1/indexerproxy')
    if any(p.get('implementation') == 'FlareSolverr' for p in (proxies or [])):
        log('  FlareSolverr: present')
        return tag['id']
    st, sch = call('/api/v1/indexerproxy/schema')
    p = next(x for x in sch if x['implementation'] == 'FlareSolverr')
    p.update(name='FlareSolverr', tags=[tag['id']])
    setfields(p, {'host': 'http://flaresolverr:8191/', 'requestTimeout': 60})
    st, r = call('/api/v1/indexerproxy', p)
    log('  FlareSolverr:', 'OK' if ok(st) else f'FAIL {st} {str(r)[:120]}')
    return tag['id']


def expand(v):
    """${VAR} -> environment, so tracker credentials stay out of indexers.toml."""
    if isinstance(v, str):
        return re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), ''), v)
    return v


def apply_indexers(call, fs_tag=None):
    """Apply every [[indexer]] in indexers.toml. Additive and idempotent: an
    entry already present is updated in place by name, a new one is added.
    Entries removed from the file are left alone, so a hand-added indexer is
    never silently destroyed."""
    import tomllib
    if not os.path.exists('indexers.toml'):
        return
    with open('indexers.toml', 'rb') as f:
        wanted = (tomllib.load(f) or {}).get('indexer') or []
    if not wanted:
        log(f'  indexers: none configured -> add some to indexers.toml, or in the UI'
            f' at {PROWLARR}. Until then nothing can be downloaded.')
        return
    st, profs = call('/api/v1/appprofile')
    apid = (profs or [{'id': 1}])[0]['id']
    added = 0
    st, sch = call('/api/v1/indexer/schema')
    st, have = call('/api/v1/indexer')
    byname = {i['name']: i for i in (have or [])}
    for e in wanted:
        d = e['definition']
        name = e.get('name', d)
        base = next((x for x in sch if x['name'] == d or x.get('definitionName') == d), None)
        if not base:
            log(f'  !! "{name}": no definition called "{d}" - check the exact spelling'
                f' in Prowlarr -> Indexers -> Add Indexer')
            continue
        r = json.loads(json.dumps(base))
        r.update(name=name, enable=e.get('enable', True),
                 priority=e.get('priority', 25), appProfileId=apid)
        # A definition's baseUrl is an unset dropdown; left empty the indexer has
        # no mirror to query. Default to the definition's first URL.
        settings = {k: expand(v) for k, v in (e.get('settings') or {}).items()}
        pinned = 'baseUrl' in settings
        if not pinned and (base.get('indexerUrls') or []):
            settings['baseUrl'] = base['indexerUrls'][0]
        # Cloudflare-protected definitions carry an info_flaresolverr field. Tag
        # them so Prowlarr routes them through FlareSolverr; without it they
        # import cleanly and then return nothing.
        needs_fs = e.get('flaresolverr', any(f['name'] == 'info_flaresolverr'
                                             for f in base.get('fields', [])))
        if needs_fs and fs_tag:
            r['tags'] = sorted({*r.get('tags', []), fs_tag})
        known = {f['name'] for f in r.get('fields', [])}
        for k in settings:
            if k not in known:
                log(f'  !! "{name}": setting "{k}" is not a field of this definition;'
                    f' valid: {sorted(known)}')
        setfields(r, settings)
        if name in byname:
            r['id'] = byname[name]['id']
            # Keep the mirror Prowlarr is already using. Re-applying the
            # definition's primary URL would undo the working mirror the add
            # path found, and the re-test of a blocked domain then fails the
            # whole update - so `make provision` would un-fix itself.
            if not pinned:
                live = next((f.get('value') for f in byname[name].get('fields', [])
                             if f['name'] == 'baseUrl'), None)
                if live:
                    setfields(r, {'baseUrl': live})
            # forceSave: this is reconciling declared config, not probing the
            # site. Testing here only re-times-out on slow Cloudflare lookups.
            st, resp = call(f'/api/v1/indexer/{r["id"]}?forceSave=true', r, 'PUT')
            log(f'  indexer "{name}": up to date' if ok(st)
                else f'  indexer "{name}": UPDATE FAILED {st} {str(resp)[:90]}')
        else:
            # Public trackers get blocked, change domain, and have their TLS
            # intercepted (a rejected certificate means a firewall or ISP is in
            # the way, not that the site is down). Definitions ship a mirror
            # list, so walk it instead of giving up on the first failure.
            mirrors = ([settings['baseUrl']] if pinned
                       else (base.get('indexerUrls') or [settings.get('baseUrl')]))
            note = '  [FlareSolverr]' if needs_fs else ''
            last = (0, '')
            for n_try, url in enumerate(mirrors):
                if url:
                    setfields(r, {'baseUrl': url})
                q = '?forceSave=true' if e.get('force') else ''
                st, resp = call('/api/v1/indexer' + q, r)
                if ok(st):
                    log(f'  indexer "{name}": added'
                        + (f' via mirror {url}' if n_try else '') + note)
                    added += 1
                    time.sleep(2)
                    break
                last = (st, resp)
            else:
                # No mirror passed its test. Save it against the first one anyway
                # rather than dropping it, and say plainly that it is not working.
                if mirrors and mirrors[0]:
                    setfields(r, {'baseUrl': mirrors[0]})
                st, resp = call('/api/v1/indexer?forceSave=true', r)
                why = str(last[1])
                hint = ('TLS intercepted - a firewall or your ISP is blocking it,'
                        ' a VPN usually fixes this' if 'certificate' in why
                        else 'every known mirror unreachable')
                log(f'  indexer "{name}": added but NOT WORKING ({hint})' if ok(st)
                    else f'  indexer "{name}": ADD FAILED {last[0]} {why[:90]}')

    if added:
        # Push everything into Sonarr/Radarr in one pass, so an indexer that lost
        # its per-add sync to a 429 still lands.
        st, _ = call('/api/v1/command', {'name': 'ApplicationIndexerSync'})
        log('  sync to Sonarr/Radarr:', 'triggered' if ok(st) else f'FAIL {st}')
        time.sleep(15)


# ================= Jellyfin =================
def jf_auth(token=None):
    """Jellyfin wants client identity in the Authorization header, token and all."""
    h = 'MediaBrowser Client="provision", Device="cli", DeviceId="provision-1", Version="1.0"'
    return h + (f', Token="{token}"' if token else '')



LIBRARIES = [('Movies', 'movies', '/media/movies'), ('Shows', 'tvshows', '/media/tv')]


def jellyfin():
    log('\n== Jellyfin ==')
    wait(JF + '/System/Info/Public', want=(200,), label='jellyfin')
    lang, country = E['JELLYFIN_LANG'], E['JELLYFIN_COUNTRY']
    user, pw = E['JELLYFIN_ADMIN_USER'], E['JELLYFIN_ADMIN_PASS']
    # A fresh Jellyfin leaves IsStartupWizardCompleted=false and /Startup/* open, so
    # the wizard is scriptable. Once ANY admin exists the server seals those
    # endpoints on every boot -- including the admin a browser visit creates for
    # itself, which is why you must not open the web UI before provisioning.
    if http(JF + '/Startup/User', raw=True)[0] == 200:
        http(JF + '/Startup/Configuration', {'UICulture': f'{lang}-US',
                                             'MetadataCountryCode': country,
                                             'PreferredMetadataLanguage': lang})
        http(JF + '/Startup/User')                    # creates the default user row
        st, r = http(JF + '/Startup/User', {'Name': user, 'Password': pw})
        log('  wizard admin user:', 'OK' if ok(st) else f'FAIL {st} {r}')
        http(JF + '/Startup/RemoteAccess', {'EnableRemoteAccess': True,
                                            'EnableAutomaticPortMapping': False})
        st, r = http(JF + '/Startup/Complete', {})
        log('  wizard complete:', 'OK' if ok(st) else f'FAIL {st} {r}')
    else:
        log('  wizard already sealed (an admin exists)')

    st, r = http(JF + '/Users/AuthenticateByName', {'Username': user, 'Pw': pw},
                 {'Authorization': jf_auth()})
    if not ok(st):
        log(f'  !! admin login failed: {st} {str(r)[:80]}')
        log('     Jellyfin has an admin whose password we do not know - usually')
        log('     because .env was regenerated after Jellyfin was already set up.')
        log('     To let this script own it again:  make nuke && make')
        return None
    H = {'Authorization': jf_auth(r['AccessToken'])}
    log('  admin login: OK')

    st, have = http(JF + '/Library/VirtualFolders', headers=H)
    names = {f['Name'] for f in (have or [])}
    for name, ctype, path in LIBRARIES:
        if name in names:
            log(f'  library {name}: present')
            continue
        q = urllib.parse.urlencode({'name': name, 'collectionType': ctype, 'refreshLibrary': 'false'})
        st, _ = http(f'{JF}/Library/VirtualFolders?{q}',
                     {'LibraryOptions': {'PathInfos': [{'Path': path}],
                                         'EnableRealtimeMonitor': True}}, H)
        log(f'  library {name} -> {path}:', 'OK' if ok(st) else f'FAIL {st}')

    server_identity(H)
    hwaccel(H)
    http(JF + '/Library/Refresh', None, H, method='POST')   # a JSON body here 500s
    # An API key for Sonarr/Radarr to poke the library with after each import.
    st, keys = http(JF + '/Auth/Keys', headers=H)
    items = (keys or {}).get('Items', []) if isinstance(keys, dict) else []
    k = next((x for x in items if x.get('AppName') == 'Arr'), None)
    if not k:
        http(f'{JF}/Auth/Keys?' + urllib.parse.urlencode({'app': 'Arr'}), {}, H)
        st, keys = http(JF + '/Auth/Keys', headers=H)
        items = (keys or {}).get('Items', []) if isinstance(keys, dict) else []
        k = next((x for x in items if x.get('AppName') == 'Arr'), None)
    log('  API key for Sonarr/Radarr:', 'OK' if k else 'FAIL')
    return (k or {}).get('AccessToken')


def notify_jellyfin(label, base, key, jf_key):
    """Tell Jellyfin to rescan the moment something is imported.

    Jellyfin's own realtime monitoring relies on inotify, which does not cross
    Docker Desktop's virtiofs and is unreliable on network shares - so a finished
    download can sit there invisible until the next scheduled scan. A push from
    Sonarr/Radarr is immediate and filesystem-independent.

    Paths must be translated: the *arr see /data/media, Jellyfin sees /media.
    Without the mapping the targeted scan points at a path Jellyfin has no
    library for, and quietly does nothing."""
    call = arr(base, key)
    st, cur = call('/api/v3/notification')
    if any(n.get('implementation') == 'MediaBrowser' for n in (cur or [])):
        log(f'  {label} -> Jellyfin: present')
        return
    st, sch = call('/api/v3/notification/schema')
    n = next(x for x in sch if x['implementation'] == 'MediaBrowser')
    n.update(name='Jellyfin', onDownload=True, onUpgrade=True, onRename=True,
             onMovieDelete=True, onMovieFileDelete=True,
             onSeriesDelete=True, onEpisodeFileDelete=True)
    setfields(n, {'host': 'jellyfin', 'port': 8096, 'useSsl': False,
                  'apiKey': jf_key, 'updateLibrary': True, 'notify': False,
                  'mapFrom': '/data/media', 'mapTo': '/media'})
    st, r = call('/api/v3/notification', n)
    log(f'  {label} -> Jellyfin library update:',
        'OK' if ok(st) else f'FAIL {st} {str(r)[:140]}')


def server_identity(H):
    """Name the server, and teach it about the reverse proxy if there is one.

    Jellyfin defaults ServerName to the container's hostname, so clients show a
    hex blob. EnablePublishedServerUriByRequest makes Jellyfin derive its own URL
    from each request's Host header, which is what lets the SAME server answer
    correctly on jellyfin.home and on nuc.local:8096 - otherwise one of the two
    hands clients links they cannot reach. KnownProxies is needed for Jellyfin to
    trust X-Forwarded-For and log real client IPs instead of the proxy's.
    """
    name = E.get('JELLYFIN_SERVER_NAME') or ''
    if name:
        st, cfg = http(JF + '/System/Configuration', headers=H)
        if ok(st) and cfg.get('ServerName') != name:
            cfg['ServerName'] = name
            st, r = http(JF + '/System/Configuration', cfg, H)
            log(f'  server name -> {name!r}:', 'OK' if ok(st) else f'FAIL {st} {str(r)[:90]}')
        else:
            log(f'  server name: {name!r} already set')
    if not PROXY:
        return
    st, net = http(JF + '/System/Configuration/network', headers=H)
    if not ok(st):
        log(f'  !! could not read network config: {st}')
        return
    before = (net.get('KnownProxies'), net.get('EnablePublishedServerUriByRequest'))
    net['KnownProxies'] = ['caddy']
    net['EnablePublishedServerUriByRequest'] = True
    if (net['KnownProxies'], net['EnablePublishedServerUriByRequest']) == before:
        log('  proxy awareness: already set')
        return
    st, r = http(JF + '/System/Configuration/network', net, H)
    log('  proxy awareness (known proxy + URL from request):',
        'OK' if ok(st) else f'FAIL {st} {str(r)[:90]}')


def hwaccel(H):
    """Intel Quick Sync. The compose overlay hands Jellyfin the device; this
    flips Jellyfin's own encoding settings over to it. GET-patch-POST, because
    POSTing a partial EncodingOptions resets every field you leave out."""
    mode = (E.get('HWACCEL') or 'none').lower()
    if mode == 'none':
        log('  transcoding: software (HWACCEL=none)')
        return
    if not os.path.exists('/dev/dri'):
        log(f'  !! HWACCEL={mode} but /dev/dri does not exist on this host'
            f' -> staying on software transcoding')
        return
    st, enc = http(JF + '/System/Configuration/encoding', headers=H)
    if not ok(st):
        log(f'  !! could not read encoding config: {st}')
        return
    enc.update(HardwareAccelerationType=mode, EnableHardwareEncoding=True,
               AllowHevcEncoding=True, EnableTonemapping=True,
               HardwareDecodingCodecs=['h264', 'hevc', 'vp9', 'av1'],
               EnableDecodingColorDepth10Hevc=True, EnableDecodingColorDepth10Vp9=True)
    if mode == 'qsv':
        enc['QsvDevice'] = '/dev/dri/renderD128'
    else:
        enc['VaapiDevice'] = '/dev/dri/renderD128'
    st, r = http(JF + '/System/Configuration/encoding', enc, H)
    log(f'  transcoding: {mode} on /dev/dri/renderD128',
        'OK' if ok(st) else f'FAIL {st} {str(r)[:120]}')


# ================= Seerr =================
# Seerr's own owner account lives in its SQLite DB, not in settings.json, so it
# cannot be seeded by file. It IS creatable over the API: signing in with a
# Jellyfin admin on a fresh install promotes that user to owner and makes Seerr
# mint its own Jellyfin API key. That is the whole wizard, scripted.
def seerr(rprof, sprof):
    log('\n== Seerr ==')
    wait(SEERR + '/api/v1/status', want=(200,), label='seerr')
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(path, data=None, method=None):
        return http(SEERR + path, data, method=method, opener=opener)

    body = {'username': E['JELLYFIN_ADMIN_USER'], 'password': E['JELLYFIN_ADMIN_PASS'],
            'email': E['SEERR_EMAIL'], 'serverType': 2,          # 2 = Jellyfin
            'hostname': 'jellyfin', 'port': 8096, 'useSsl': False, 'urlBase': ''}
    st, r = call('/api/v1/auth/jellyfin', body)
    if not ok(st) and 'already configured' in str(r):
        # Re-run: Seerr knows the Jellyfin host, and passing it again is rejected.
        st, r = call('/api/v1/auth/jellyfin', {k: v for k, v in body.items()
                                               if k not in ('hostname', 'port', 'useSsl', 'urlBase')})
    if not ok(st):
        log(f'  !! Jellyfin sign-in failed: {st} {str(r)[:160]}')
        log(f'     Finish by hand at {SEERR} with the Jellyfin admin login.')
        return
    log(f'  owner account: {r.get("displayName") or E["JELLYFIN_ADMIN_USER"]}'
        f' (signed in via Jellyfin)')

    st, libs = call('/api/v1/settings/jellyfin/library/sync', {})
    if not ok(st):
        log(f'  !! library sync failed: {st} {str(libs)[:120]}')
        libs = []
    wanted = {name for name, _c, _p in LIBRARIES}
    for lib in (libs or []):
        if lib['name'] in wanted and not lib.get('enabled'):
            st, _ = call(f'/api/v1/settings/jellyfin/library/{lib["id"]}', {'enabled': True}, 'PUT')
            log(f'  library "{lib["name"]}": enabled' if ok(st) else f'  library "{lib["name"]}": FAIL {st}')
        elif lib['name'] in wanted:
            log(f'  library "{lib["name"]}": already enabled')

    common = {'useSsl': False, 'baseUrl': '', 'is4k': False, 'isDefault': True,
              'externalUrl': '', 'syncEnabled': True, 'preventSearch': False,
              'tags': [], 'tagRequests': False, 'overrideRule': []}
    if rprof:
        st, cur = call('/api/v1/settings/radarr')
        if cur:
            log('  Radarr: present')
        else:
            st, r = call('/api/v1/settings/radarr', {
                'name': 'Radarr', 'hostname': 'radarr', 'port': 7878,
                'apiKey': E['RADARR_API_KEY'], 'activeProfileId': rprof['profileId'],
                'activeProfileName': rprof['profileName'], 'activeDirectory': '/data/media/movies',
                'minimumAvailability': 'released', **common})
            log('  Radarr:', 'OK' if ok(st) else f'FAIL {st} {str(r)[:120]}')
    if sprof:
        st, cur = call('/api/v1/settings/sonarr')
        if cur:
            log('  Sonarr: present')
        else:
            st, r = call('/api/v1/settings/sonarr', {
                'name': 'Sonarr', 'hostname': 'sonarr', 'port': 8989,
                'apiKey': E['SONARR_API_KEY'], 'activeProfileId': sprof['profileId'],
                'activeProfileName': sprof['profileName'], 'activeDirectory': '/data/media/tv',
                'activeAnimeProfileId': sprof['profileId'],
                'activeAnimeProfileName': sprof['profileName'],
                'activeAnimeDirectory': '/data/media/tv',
                'seriesType': 'standard', 'animeSeriesType': 'anime',
                'enableSeasonFolders': True, 'monitorNewItems': 'all', **common})
            log('  Sonarr:', 'OK' if ok(st) else f'FAIL {st} {str(r)[:120]}')

    call('/api/v1/settings/main', {'applicationTitle': 'Seerr',
                                   'applicationUrl': E['SEERR_PUBLIC_URL']}, 'POST')
    st, _ = call('/api/v1/settings/initialize', {})
    log('  setup marked complete:', 'OK' if ok(st) else f'FAIL {st}')


# ================= main =================
def write_docs():
    """Everything needed to log in, in one gitignored file."""
    rows = [
        ('Jellyfin', JF, 'http://jellyfin:8096', E['JELLYFIN_ADMIN_USER'],
         E['JELLYFIN_ADMIN_PASS'], 'playback'),
        ('Seerr', SEERR, 'http://seerr:5055', E['JELLYFIN_ADMIN_USER'],
         '(the Jellyfin password)', 'request UI - this is the one you share'),
        ('Prowlarr', PROWLARR, 'http://prowlarr:9696', '(no login on LAN)', '-',
         'indexers live here'),
        ('Sonarr', SONARR, 'http://sonarr:8989', '(no login on LAN)', '-', 'series'),
        ('Radarr', RADARR, 'http://radarr:7878', '(no login on LAN)', '-', 'movies'),
        ('qBittorrent', QB, f'http://{QB_HOST}:{QB_PORT}', 'admin',
         E['QBITTORRENT_PASS'], 'transport'),
    ]
    L = ['# Credentials and endpoints', '',
         'Generated by `provision.py`. Gitignored. Re-run `make provision` to regenerate.', '',
         '| Service | Browser URL | In-container URL | User | Password | Role |',
         '|---|---|---|---|---|---|']
    L += [f'| {n} | {u} | {i} | `{usr}` | `{pw}` | {role} |' for n, u, i, usr, pw, role in rows]
    L += ['', '## API keys', ''] + [
        f'- {k.split("_")[0].title()}: `{E[k]}`'
        for k in ('PROWLARR_API_KEY', 'SONARR_API_KEY', 'RADARR_API_KEY', 'SEERR_API_KEY')]
    L += ['', '## Notes', '',
          '- These values come from `.env`. Back that file up; losing it means',
          '  re-keying every app.',
          '- Do NOT open Jellyfin in a browser before the first `make` finishes: the',
          '  visit makes Jellyfin create its own admin and seal the setup endpoints,',
          '  which locks `provision.py` out.',
          f'- Sonarr/Radarr/Prowlarr have no login (ARR_AUTH_METHOD={E["ARR_AUTH_METHOD"]}).',
          '  Anyone on the LAN can open them. See .env for how to turn that off.', '']
    open('CREDENTIALS.md', 'w').write('\n'.join(L))
    os.chmod('CREDENTIALS.md', 0o600)
    log('\n  wrote CREDENTIALS.md (chmod 600, gitignored)')


# ================= check =================
def check():
    """`make check`: assert the stack is actually wired up, rather than trusting
    that provisioning printed OK. Exits non-zero on the first real problem."""
    bad = []

    def t(label, cond, detail=''):
        log(f'  {"PASS" if cond else "FAIL"}  {label}' + (f'  [{detail}]' if not cond and detail else ''))
        if not cond:
            bad.append(label)

    log('\n== reachable ==')
    for label, url in (('jellyfin', JF + '/System/Info/Public'), ('seerr', SEERR + '/api/v1/status'),
                       ('prowlarr', PROWLARR + '/api/v1/system/status'),
                       ('sonarr', SONARR + '/api/v3/system/status'),
                       ('radarr', RADARR + '/api/v3/system/status'), ('qbittorrent', QB + '/')):
        t(label, http(url, raw=True)[0] in (200, 401, 403))

    log('\n== qBittorrent ==')
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    st, _ = http(QB + '/api/v2/auth/login', {'username': 'admin', 'password': E['QBITTORRENT_PASS']},
                 {'Referer': QB}, form=True, raw=True, opener=opener)
    t('login with QBITTORRENT_PASS', ok(st), f'http {st}')
    st, cats = http(QB + '/api/v2/torrents/categories', opener=opener)
    t('categories radarr + sonarr', {'radarr', 'sonarr'} <= set(cats or {}), str(cats)[:60])

    for label, base, key, root in (('Radarr', RADARR, E['RADARR_API_KEY'], '/data/media/movies'),
                                   ('Sonarr', SONARR, E['SONARR_API_KEY'], '/data/media/tv')):
        log(f'\n== {label} ==')
        call = arr(base, key)
        st, rf = call('/api/v3/rootfolder')
        t(f'{label} API key accepted', ok(st), f'http {st}')
        t(f'{label} root folder {root}', any(f.get('path') == root for f in (rf or [])))
        st, dc = call('/api/v3/downloadclient')
        t(f'{label} -> qBittorrent enabled',
          any(c.get('implementation') == 'QBittorrent' and c.get('enable') for c in (dc or [])))

    log('\n== Prowlarr ==')
    call = arr(PROWLARR, E['PROWLARR_API_KEY'])
    st, apps = call('/api/v1/applications')
    t('Prowlarr API key accepted', ok(st), f'http {st}')
    t('Prowlarr -> Sonarr + Radarr', {'Sonarr', 'Radarr'} <= {a['name'] for a in (apps or [])})
    st, prox = call('/api/v1/indexerproxy')
    t('FlareSolverr registered', any(p.get('implementation') == 'FlareSolverr' for p in (prox or [])))
    st, idx = call('/api/v1/indexer')
    t('at least one indexer', bool(idx), 'indexers.toml empty or all adds failed')
    for i in (idx or []):
        t(f'indexer {i["name"]} enabled', i.get('enable'))
    # Prowlarr pushes indexers into Sonarr/Radarr; if that never happened they
    # warn "No indexers available with RSS sync enabled" and grab nothing.
    for label, base, key in (('Sonarr', SONARR, E['SONARR_API_KEY']),
                             ('Radarr', RADARR, E['RADARR_API_KEY'])):
        st, synced = http(base + '/api/v3/indexer', headers={'X-Api-Key': key})
        t(f'{label} received indexers from Prowlarr', bool(synced),
          'run `make provision` again; Prowlarr syncs on a delay')
        t(f'{label} has RSS + automatic search',
          any(i.get('enableRss') and i.get('enableAutomaticSearch') for i in (synced or [])))

    log('\n== storage ==')
    # Imports are hardlinks. If /data/torrents and /data/media are not the same
    # filesystem the link fails, Radarr silently falls back to copying, and every
    # film costs disk twice. Cheap to assert, expensive to discover later.
    probe = ("T=/data/torrents/.hardlink-probe; M=/data/media/movies/.hardlink-probe; "
             "rm -f $T $M; echo x > $T; "
             "ln $T $M 2>/dev/null && [ \"$(stat -c %i $T)\" = \"$(stat -c %i $M)\" ] "
             "&& echo LINKOK; rm -f $T $M")
    out = subprocess.run([*COMPOSE, 'exec', '-T', 'radarr', 'sh', '-c', probe],
                         capture_output=True, text=True)
    t('torrents and media on one filesystem (hardlinks work)', 'LINKOK' in out.stdout,
      'imports will be copied instead, doubling disk use')

    log('\n== Jellyfin ==')
    st, r = http(JF + '/Users/AuthenticateByName',
                 {'Username': E['JELLYFIN_ADMIN_USER'], 'Pw': E['JELLYFIN_ADMIN_PASS']},
                 {'Authorization': jf_auth()})
    t('admin login', ok(st), f'http {st}')
    if ok(st):
        H = {'Authorization': jf_auth(r['AccessToken'])}
        st, folders = http(JF + '/Library/VirtualFolders', headers=H)
        have = {f['Name']: f['Locations'] for f in (folders or [])}
        for name, _c, path in LIBRARIES:
            t(f'library {name} -> {path}', path in have.get(name, []), str(have.get(name)))
        mode = (E.get('HWACCEL') or 'none').lower()
        if mode != 'none' and os.path.exists('/dev/dri'):
            st, enc = http(JF + '/System/Configuration/encoding', headers=H)
            t(f'hardware transcoding = {mode}', (enc or {}).get('HardwareAccelerationType') == mode,
              str((enc or {}).get('HardwareAccelerationType')))

    for label, base, key in (('Sonarr', SONARR, E['SONARR_API_KEY']),
                             ('Radarr', RADARR, E['RADARR_API_KEY'])):
        st, notif = http(base + '/api/v3/notification', headers={'X-Api-Key': key})
        t(f'{label} pushes library updates to Jellyfin',
          any(n.get('implementation') == 'MediaBrowser' and n.get('onDownload')
              for n in (notif or [])),
          'imports would wait for Jellyfin to notice them by itself')

    log('\n== Seerr ==')
    st, pub = http(SEERR + '/api/v1/settings/public')
    t('setup initialized', bool((pub or {}).get('initialized')))
    t('media server = Jellyfin', (pub or {}).get('mediaServerType') == 2,
      str((pub or {}).get('mediaServerType')))
    K = {'X-Api-Key': E['SEERR_API_KEY']}
    st, jfs = http(SEERR + '/api/v1/settings/jellyfin', headers=K)
    t('Seerr API key accepted', ok(st), f'http {st}')
    t('Jellyfin linked with its own key', bool((jfs or {}).get('apiKey')))
    enabled = {l['name'] for l in (jfs or {}).get('libraries', []) if l.get('enabled')}
    t('both libraries synced', {n for n, _c, _p in LIBRARIES} <= enabled, str(enabled))
    for svc in ('radarr', 'sonarr'):
        st, cfg = http(SEERR + f'/api/v1/settings/{svc}', headers=K)
        t(f'Seerr -> {svc}', bool(cfg) and any(c.get('isDefault') for c in cfg))

    log('')
    if bad:
        log(f'  {len(bad)} check(s) failed: {", ".join(bad)}')
        sys.exit(1)
    log('  all checks passed')


def main():
    qbittorrent()
    rprof = arr_app('Radarr', RADARR, E['RADARR_API_KEY'], '/data/media/movies',
                    'radarr', 'HD-1080p', 'ARR')
    sprof = arr_app('Sonarr', SONARR, E['SONARR_API_KEY'], '/data/media/tv',
                    'sonarr', 'HD-1080p', 'TV')
    prowlarr()
    jf_key = jellyfin()
    if jf_key:
        notify_jellyfin('Radarr', RADARR, E['RADARR_API_KEY'], jf_key)
        notify_jellyfin('Sonarr', SONARR, E['SONARR_API_KEY'], jf_key)
        seerr(rprof, sprof)
    write_docs()
    log(f'\n  done. Share {E["SEERR_PUBLIC_URL"]} with the household,'
        f' watch at {E["JELLYFIN_PUBLISHED_URL"]}')


def torrents():
    """`make shell-qbt`: what every torrent is doing and what each tracker says.
    A state of metaDL with size 0 means no peer was ever reached, which is a
    reachability problem rather than a bad release."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar()))
    st, _ = http(QB + '/api/v2/auth/login',
                 {'username': 'admin', 'password': E['QBITTORRENT_PASS']},
                 {'Referer': QB}, form=True, raw=True, opener=opener)
    if not ok(st):
        sys.exit(f'qBittorrent login failed ({st})')
    st, info = http(QB + '/api/v2/transfer/info', opener=opener)
    log(f'connection: {info.get("connection_status")}   dht nodes: {info.get("dht_nodes")}')
    if info.get('dht_nodes') == 0:
        log('  !! DHT has no nodes. DHT is UDP; if UDP is filtered there is no'
            ' peer source left once the trackers fail too.')
    st, ts = http(QB + '/api/v2/torrents/info', opener=opener)
    if not ts:
        log('no torrents')
        return
    for t in ts:
        log(f'\n{t["name"][:78]}')
        log(f'  state={t["state"]} progress={t["progress"]*100:.1f}%'
            f' size={(t["size"] or 0)/2**30:.2f}GiB seeds={t["num_seeds"]}'
            f' peers={t["num_leechs"]} dl={t["dlspeed"]/1000:.0f}kB/s')
        if t['state'] == 'metaDL':
            log('  metaDL = not one peer reached yet, so even the file list is'
                ' unknown. Look at the tracker messages below, not at the release.')
        st, trs = http(QB + '/api/v2/torrents/trackers?hash=' + t['hash'], opener=opener)
        for tr in trs or []:
            if tr['url'].startswith('**'):          # DHT/PeX/LSD pseudo-entries
                continue
            log(f'    {tr["msg"][:46]!r:50} {tr["url"][:62]}')


if __name__ == '__main__':
    if '--check' in sys.argv:
        check()
    elif '--torrents' in sys.argv:
        torrents()
    else:
        main()
