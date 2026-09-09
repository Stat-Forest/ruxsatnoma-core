# e-imzo-server (plan 05.2, task 8)

The E-IMZO signature/verification server, v2.1.1, running as an in-stack container. It
must never be reachable from the internet (plan ruling R2): it joins only the stack's
private `default` network, never the `edge` network Caddy uses to reach `api` from
outside. Our own API is the only thing that talks to it, through the two proxy routes
in `app/modules/integrations/router.py` (`POST /api/v1/eimzo/timestamp`,
`GET /api/v1/eimzo/health`, task 7).

## What you need before this runs for real

Three files, all issued by **NIC** (ScientificInformationCenter, the operator of
e-imzo.uz) per domain, none of them public downloads and none of them ever committed
to this repository (`.gitignore` in this directory covers `keys/` and `*.jar`):

| File (mounted at `keys/` inside the container) | What it is |
|---|---|
| `keys/client.key` | The VPN client key NIC issues for this server -- what `vpn.key.file.path` in `config.properties` points at. `vpn.key.password` (env `EIMZO_VPN_KEY_PASSWORD`) unlocks it. |
| `keys/vpn.jks` | The truststore for the VPN connection itself (`vpn.truststore.file.path`). |
| `keys/truststore.jks` | The TSP (timestamp authority) truststore (`tsp.jks.file.path`) -- what `RealEimzo.attach_timestamp` (task 3/7) ultimately depends on answering. |

`e-imzo-server.jar` itself is a **fourth** proprietary artifact, distributed by NIC the
same way (not a public download -- verified against the vendor's own documentation
repository, which ships no Docker image or jar URL either). Place it at
`deploy/eimzo/e-imzo-server.jar` before running `docker compose build eimzo`; the build
fails at the `COPY e-imzo-server.jar` step without it.

Until all four exist, this is exactly as far as things get: the container **builds and
starts**, e-imzo-server itself comes up and answers `/ping`, but every VPN-dependent
call -- every login, every signature verification, every timestamp -- refuses with
"certificate status could not be verified" (status `-1`) because it cannot reach the
VPN at all. That is the correct, expected, fail-closed state until NIC delivers --
never something to work around with a stub key.

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
key's own validity window lives. The exact field name in `info` is confirmed only once
a live server answers it (plan 05.2 task 12 is blocked on NIC delivering a key at all)
-- read the JSON body it actually returns and look for a validity/expiry timestamp near
the key or certificate section. From inside the stack, the same two calls work directly
against the container, without going through our API at all:

```bash
docker compose exec api curl -s http://eimzo:8080/ping
docker compose exec api curl -s http://eimzo:8080/info
```

A key that has expired reads the same way a missing one does from `/ping`'s point of
view -- a VPN the server cannot use -- so `/info`'s validity window is the only way to
tell "not delivered yet" from "delivered, and now expired" apart before the next
signature attempt fails.

## Local verification (no keys required)

```bash
docker compose up -d eimzo
sleep 20
curl -s localhost:8080/ping
```

This is exactly the state described above: the container is up, `/ping` answers, and
the answer says the VPN cannot be used. It is not a failure of this task -- it is the
system correctly reporting that NIC has not delivered yet.
