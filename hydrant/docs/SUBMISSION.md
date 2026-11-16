# Submission checklist (deadline: 9 AM IST, 16 Nov)

The hackathon wants three things: a live link, an open-source GitHub repo, and a Loom.

## 1. GitHub repo (10 minutes)

```bash
cd hydrant
git init && git add . && git commit -m "Hydrant: the agent context layer that learns, on HydraDB"
gh repo create hydrant --public --source . --push      # or create it on github.com and push
```

## 2. Live link (30 to 60 minutes, most of it waiting for the HydraDB build)

Any Linux VM with 2 vCPU and 4 GB RAM and Docker works (a DigitalOcean, Hetzner, GCP or AWS box).

```bash
git clone https://github.com/<you>/hydrant && cd hydrant
export HYDRADB_TOKEN=$(openssl rand -hex 24) HYDRANT_ADMIN_TOKEN=$(openssl rand -hex 12) PORT=80
docker compose up -d --build        # first run compiles HydraDB from source: 20 to 40 minutes
curl localhost/api/health           # {"ok": true, "store": "hydradb", "tools": 435}
```

Open `http://<vm-ip>/`. With `HYDRANT_ADMIN_TOKEN` set, the "Reset graph" button on the public page is refused,
which is what you want while judges are using it. Reset yourself with:
`curl -X POST localhost/api/admin/reset -H "X-Admin-Token: $HYDRANT_ADMIN_TOKEN"`.

The compose file has not been run end to end (the build sandbox had no Docker). If the HydraDB image build
fails, the tested path is on the same VM without Docker:

```bash
bash scripts/run_hydradb.sh &                          # builds and runs graph-node + graph-indexer
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
uvicorn hydrant.api:app --host 0.0.0.0 --port 80
```

Before recording or sharing: click "Replay 50 tasks of usage" a few times so visitors land on a graph that has
already learned something, or leave it fresh if you want to show the climb live.

## 3. Loom (15 minutes)

Script in `docs/LOOM.md`. Four minutes, six beats.

## 4. Posts

Drafts in `docs/PITCH.md`. Fill in the live link and repo link, tag the HydraDB account, and post on X and
LinkedIn before submitting: go-to-market is half the score.

## 5. Ask in Discord

Whether one submission can be judged in both tracks. If not, enter Track 2 and present the learning loop as
what sets it apart.
