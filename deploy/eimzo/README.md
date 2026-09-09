# e-imzo-server (plan 05.2, task 8; fix round 1)

The E-IMZO signature/verification server, v2.1.1, running as an in-stack container. It
must never be reachable from the internet (plan ruling R2): it joins only the stack's
private `default` network, never the `edge` network Caddy uses to reach `api` from
outside. Our own API is the only thing that talks to it, through the two proxy routes
in `app/modules/integrations/router.py` (`POST /api/v1/eimzo/timestamp`,
`GET /api/v1/eimzo/health`, task 7).

`Dockerfile` is built directly from **the vendor's own Dockerfile**, shipped inside
the distribution zip alongside the jar -- not from guesswork. Every place this one
differs from the vendor's is commented at the point it applies.

## What you need before this runs at all

NIC (ScientificInformationCenter, the operator of e-imzo.uz) distributes the whole
`e-imzo-server` package together, as one zip -- **it is not "unobtainable"; it ships
with a jar, a `lib/` directory of dependencies, sample config, and NIC's own
Dockerfile.** None of it is a public download, and none of it is ever committed to
this repository (`.gitignore` in this directory covers `keys/`, `dist/` and `*.jar`).

| Path (relative to this directory) | What it is |
|---|---|
| `dist/e-imzo-server.jar` | The server itself. **A thin jar** -- its manifest's `Class-Path` lists every dependency in `lib/` by name, so it cannot even finish loading its own main class without that directory sitting alongside it (fix round 1, critical finding 1: the previous Dockerfile never copied `lib/` at all, and the jar died with `NoClassDefFoundError` before config, VPN or `/ping` were ever reached). |
| `dist/lib/` | The ~94 dependency jars named in the manifest's `Class-Path`. Copy the whole directory from the distribution -- do not hand-pick entries. |
| `keys/client.key` | The VPN client key NIC issues for this server -- what `vpn.key.file.path` in `config.properties` points at. `vpn.key.password` (env `EIMZO_VPN_KEY_PASSWORD`) unlocks it. |
| `keys/vpn.jks` | The truststore for the VPN connection itself (`vpn.truststore.file.path`). |
| `keys/truststore.jks` | The TSP (timestamp authority) truststore (`tsp.jks.file.path`) -- what `RealEimzo.attach_timestamp` (task 3/7) ultimately depends on answering. |

Assemble the jar and `lib/` under `dist/` before running `docker compose build eimzo`:

```
deploy/eimzo/dist/e-imzo-server.jar
deploy/eimzo/dist/lib/*.jar        (~94 files, copied whole from the distribution)
```

The build fails loudly at the `COPY dist/e-imzo-server.jar` / `COPY dist/lib` steps
without them -- an operator who forgot the distribution sees a build error, not a
container that starts and quietly does nothing. `config.properties` and
`logging.properties` in this directory are NOT proprietary -- they ship in this repo,
copied from the vendor's own distribution (task 8, fix round 1 finding 4) with our own
`${EIMZO_VPN_HOST}`-style placeholders substituted in at container start
(`docker-entrypoint.sh`).

## `keys/` and `dist/` are operator-owned and survive deploys (Critical 1 & 2, final review)

Both directories are gitignored (`.gitignore`, this directory), so `git archive` --
what `.github/workflows/deploy.yml` ships to the server -- never contains either one.
Two consequences an operator needs to know:

- **This service sits behind the `eimzo` Docker Compose profile**
  (`docker-compose.yml`/`docker-compose.deploy.yml`), so a bare `docker compose
  build`/`up -d` -- what `make up` and every deploy run by default -- never even
  attempts to build it, and cannot fail on a missing `dist/`. The deploy workflow
  activates the profile itself (`COMPOSE_PROFILES=eimzo`) the moment it finds
  `deploy/eimzo/dist/e-imzo-server.jar` already on the server, so placing the
  distribution there is the ONLY step needed -- no workflow change, no manual
  `--profile` flag on the next deploy. Locally, `docker compose build eimzo` /
  `docker compose up -d eimzo` (below) still reach it by name regardless of the
  profile, exactly as before.
- **`.github/workflows/deploy.yml`'s `rsync -a --delete` explicitly excludes
  `deploy/eimzo/keys/` and `deploy/eimzo/dist/`** (alongside the pre-existing
  `.env` exclude), so an operator's hand-placed VPN key, its truststores, and the
  assembled jar+`lib/` survive every later deploy untouched. Before this exclude,
  the NEXT deploy after an operator set these up would have deleted them straight
  back out -- `--delete` makes the destination match the shipped archive exactly,
  and an archive built from git can never contain a gitignored directory -- so
  signing would answer 502 (the container back to `unhealthy`, see below) until
  someone noticed and had to re-place both by hand, forever, on every deploy.

## What actually happens with no VPN key -- measured, not inferred

**This is the part an administrator will be stopped by if they skip it.** With
`keys/` empty (today's state, until NIC delivers), the container does **not** start
cleanly and answer `/ping` with a graceful "certificate status could not be verified".
Measured directly, building the real jar and `lib/` from the vendor's own v2.1.1
distribution and running the container with no VPN key present at all:

1. `docker compose build eimzo` -- **succeeds.**
2. `docker compose up -d eimzo` -- the container **starts and stays up**, but the
   process inside it never finishes booting: `uz.eimzo.server.cmd.Start.run` calls
   `HandlerRegistry.register`, which wires every HTTP handler including `/ping`
   itself, and that wiring eagerly constructs a `VpnNotifier` → `ManagedChannelProvider`
   that opens `keys/client.key` right there -- with no such file, it throws
   `FileNotFoundException: keys/client.key`, Guice reports a `ProvisionException`, and
   `Start.run` never binds port 8080 at all. Only the metrics listener on 8081 (started
   a step earlier) comes up. The JVM itself does not exit -- that stray thread keeps it
   alive -- so the container never crash-loops; it just sits there with nothing
   listening on 8080.
3. `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/ping` → **`000`**
   (curl: "Empty reply from server", exit code 52) -- there is no HTTP server to answer
   at all, not a non-1 status in a 200 body.
4. `GET /info` fails identically, for the identical reason.
5. The compose healthcheck (`wget -qO- http://127.0.0.1:8080/ping`) therefore **never
   passes**, and the container settles into Docker's `unhealthy` state and stays there
   permanently -- not "starting", not "eventually healthy once the sweep retries",
   permanently unhealthy until a real key is mounted and the container restarted.

This is why `docker-compose.deploy.yml`'s `api` service depends on `eimzo` with
`condition: service_started`, not `service_healthy` (fix round 1, important finding
6): `service_healthy` would block `api` from starting at all for as long as the key is
missing -- which is exactly today, and for an unknown further stretch until NIC
delivers -- turning a provider outage into an outage of the *entire* system, including
every citizen-facing feature that never touches E-IMZO. `service_started` only waits
for the container to exist and be reachable by name; the two proxy routes that
actually call it (`app/modules/integrations/router.py`, task 7) already turn a
provider outage into `ERR-INT-001`/`ERR-INT-002` (503/502) instead of a 500, so a down
E-IMZO server costs only signing and login-via-E-IMZO, never the rest of the API.

**What this measurement does NOT cover:** a `keys/client.key` file that exists but is
expired, wrong-environment (see below), or otherwise rejected by the VPN itself. That
path never reaches `FileNotFoundException` and may behave completely differently
(plan 05.2's own prior research describes a `/ping` answering HTTP 200 with
`status: -1`, "certificate status could not be verified", for that case) -- unverified
here because no real key exists on this machine to test it with. Re-verify this note
the day task 12 has a real key.

## TEST and PRODUCTION keys are not interchangeable (finding 8)

NIC issues two *kinds* of VPN key, matched to two different VPN endpoints:

- a **TEST** key, paired with a **test** signing certificate (a test PFX, e.g. one
  generated at `https://test.e-imzo.uz/registrator/public/pfx.html`), which only
  works against the **test** VPN server (`EIMZO_VPN_HOST=testvpn.e-imzo.uz`,
  `EIMZO_VPN_PORT=2443` -- the values plan 05.2's task 12 uses);
- a **PRODUCTION** key, paired with a real citizen/organization certificate, which
  only works against the **production** VPN server.

Pointing a test key at the production VPN host, or a production key at the test VPN
host, does not "mostly work" -- it fails outright, with the provider's own error
**`ocsp url is disallowed`**, on both signing and verification. If you see that error,
the first thing to check is not the signature or the certificate: it is whether
`EIMZO_VPN_HOST`/`EIMZO_VPN_PORT` in `.env` actually match the key mounted at
`keys/client.key`.

Never mix a key from one environment with the compose stack pointed at the other --
including "just to test the wiring": the whole point of `ocsp url is disallowed` is
that the provider refuses to tell you anything more specific than that.

## Reading `/info` for the key's expiry

`GET /api/v1/eimzo/health` (task 7, `sys_admin` only) proxies the provider's own
`/ping` and `/info` verbatim, so an administrator can check both without shell access
to the server:

```bash
curl -s -b "session=<cookie>" https://<api-host>/api/v1/eimzo/health | jq .
```

`ping` tells you whether the VPN is currently reachable at all; `info` is where the
key's own validity window lives -- once a real key is mounted, since the section
above shows `/info` cannot answer at all before then. The exact field name in `info`
is confirmed only once a live server with a real key answers it (plan 05.2 task 12 is
blocked on NIC delivering one) -- read the JSON body it actually returns and look for
a validity/expiry timestamp near the key or certificate section. From inside the
stack, the same two calls work directly against the container, without going through
our API at all:

```bash
docker compose exec api curl -s http://eimzo:8080/ping
docker compose exec api curl -s http://eimzo:8080/info
```

## Local verification

Naming the service explicitly (`... eimzo`, both commands below) reaches it
regardless of the `eimzo` compose profile (previous section) -- profile or no,
Compose always includes a service given by name on the command line. A bare
`docker compose build`/`up -d` would skip it instead.

```bash
# 1. Assemble the distribution (once; never committed):
mkdir -p deploy/eimzo/dist
cp /path/to/e-imzo-server.jar deploy/eimzo/dist/
cp -r /path/to/lib deploy/eimzo/dist/lib

# 2. Build and start with no keys:
docker compose build eimzo
docker compose up -d eimzo
docker compose logs eimzo

# 3. See "What actually happens with no VPN key" above for what to expect:
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/ping   # 000, empty reply
docker compose ps eimzo                                               # settles "unhealthy"
```

This is the correct, expected, fail-closed state until NIC delivers a real key --
never something to work around with a stub file.
