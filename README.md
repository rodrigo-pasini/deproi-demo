🇺🇸 English · [🇧🇷 Português](README.pt-BR.md)

# Deproi demo

A small project deployed with **[Deproi](https://github.com/rodrigo-pasini/deproi)** — the simple automatic deployer. Push to `main`, and the app is rebuilt, migrated, recreated and health-checked on its server, with automatic rollback.

Use it as a reference for what a project looks like once Deproi is in place, or fork it to try Deproi on your own server.

> **Status:** deploys are paused (`DEPROI_PAUSED=true` in the `production` environment) until a server is attached. Every push shows "paused" in Actions and connects to nothing.

## What is in here

| File | Role |
|---|---|
| [`deploy/deproi.py`](deploy/deproi.py) | Deproi itself, copied unchanged from a release. Updated by pull requests from `deproi-update.yml`. |
| [`.github/workflows/deploy-production.yml`](.github/workflows/deploy-production.yml) | Rendered by Deproi's `render-workflow.sh`, not edited. It only opens SSH; the server decides what runs. |
| [`.github/workflows/deproi-update.yml`](.github/workflows/deproi-update.yml) | Weekly: opens a pull request when a new Deproi release exists, after verifying its checksum. |
| [`compose.yaml`](compose.yaml) | Profile `prod`, suffix `-prod`, `image: deproi-demo:${DEPROI_TAG}`, `init: true`, restart policy. |
| [`Dockerfile`](Dockerfile) | `HEALTHCHECK` on `/health`: Deproi decides between success and rollback from it. |
| [`deploy/prod.compose.env`](deploy/prod.compose.env) | `COMPOSE_PROJECT_NAME` and other non-secret values. |
| [`docs/deploy/deproi-demo-prod.json`](docs/deploy/deproi-demo-prod.json) | The server config, `/etc/deproi/deproi-demo-prod.json`: a `migrate` hook and a protected worker. |
| [`app/`](app/) | `server.py` (web, `/health`), `migrate.py` (the migrate hook), `worker.py` (a protected service). |

## How a deploy works here

1. A push to `main` runs **Deploy production**.
2. The job connects as `deproi@server` with a key that can only run `sudo -n /usr/local/sbin/deproi-deproi-demo-prod`.
3. On the server, Deproi fast-forwards `/srv/deproi-demo-prod`, builds `deproi-demo:<commit>`, runs `python migrate.py` on the new image, recreates `web-prod` (and `worker-prod` when it is idle), and waits for the health check.
4. Healthy → `result: SUCCESS`. Not healthy → the previous image comes back → `result: FAILED_ROLLED_BACK`.

Commit an empty file `app/BROKEN` to see a rollback: that version's `/health` returns 500.

## Try it on your own server

1. **Fork** this repository.
2. Follow Deproi's [installation guide](https://github.com/rodrigo-pasini/deproi/blob/main/docs/installation.md) with `--name deproi-demo-prod`, checkout `/srv/deproi-demo-prod`, branch `main`, GitHub environment `production`, and [`docs/deploy/deproi-demo-prod.json`](docs/deploy/deproi-demo-prod.json) as the config (change `checkout.owner` to your user).
3. Delete the `DEPROI_PAUSED` variable from the `production` environment and push.

The app answers on port 8080 of the server: `curl http://<server>:8080/`.

**Without a server:** from a clone of Deproi, rehearse the whole thing in a local container:

```bash
scripts/try-locally.sh --project ../deproi-demo --config ../deproi-demo/docs/deploy/deproi-demo-prod.json
```

## License

[MIT](LICENSE).
