# Deployment

Two halves, deployed separately, for one hard technical reason: Vercel runs
short-lived functions with no persistent background process, and this tool's
actual OCR work (Tesseract + OpenCV, several minutes per booklet) needs a
real always-on process. So:

- **`webapp/api`** (this backend: the HTTP API + the worker) - runs on your
  EC2 box, as two containers from one image (`webapp/api/Dockerfile`).
- **`webapp/frontend`** (not yet built) - the Next.js app DES actually use,
  on Vercel, talking to the API over HTTPS.

## Backend: on your EC2 instance

Needs Docker + Docker Compose installed on the box (`sudo apt install
docker.io docker-compose-plugin` on Ubuntu, or the Amazon Linux equivalent).

```bash
git clone <this repo> && cd Extractor/webapp
cp .env.example .env      # fill in POSTGRES_PASSWORD and JWT_SECRET - see below
docker compose up -d --build
```

`.env` (not committed - real secrets):
```
POSTGRES_PASSWORD=<anything long and random>
JWT_SECRET=<anything long and random - this signs DES login sessions>
```

That's a complete working deployment on the box itself: Postgres, the API on
port 8000, and the worker, all `restart: unless-stopped` so a reboot doesn't
lose the queue.

**Two things still needed for a real deployment, both yours to decide since
they depend on what the CTO already has:**

1. **A public HTTPS URL for the API**, since Vercel (and DES' browsers) need
   to reach it over the internet, not just from inside the box. Simplest
   path on a single EC2 instance: put [Caddy](https://caddyserver.com/) in
   front of it - it gets a real TLS certificate automatically, no manual
   certbot dance:
   ```
   # /etc/caddy/Caddyfile
   api.yourdomain.com {
       reverse_proxy localhost:8000
   }
   ```
   Needs a domain (or subdomain) pointed at the instance's IP, and the
   security group open on 80/443 (not 8000 - that stays internal).

2. **Object storage**, once this is more than a single box - S3 is the
   obvious choice since the AWS account is already there:
   ```
   STORAGE_BACKEND=s3
   S3_BUCKET=akowe-extraction-review
   S3_ACCESS_KEY=...
   S3_SECRET_KEY=...
   S3_ENDPOINT_URL=            # leave blank for real AWS S3 (only needed for R2/MinIO-style endpoints)
   S3_REGION=us-east-1
   ```
   Until then, `STORAGE_BACKEND=local` (the default) works fine and keeps
   uploaded PDFs/snapshot images on the box's own disk (the `local_files`
   volume in docker-compose.yml) - completely fine for getting started,
   just means backups/durability are only as good as the instance's disk.

**Database**: `docker compose`'s own Postgres container is enough to start.
If backups/failover matter more than the cost, point `DATABASE_URL` at RDS
Postgres instead and drop the `db` service - `webapp/api/db.py` doesn't know
or care which it's talking to.

## Creating DES accounts

No self-serve signup (see `api/security.py`) - an admin runs this once per
person:

```bash
docker compose exec api python -m webapp.api.seed_users add jane@akowe.example "Jane Doe"
```

## Feeding corrections back into the tool

Once DES have been correcting flagged rows for a while:

```bash
docker compose exec api python -m webapp.api.mine_corrections                     # report only
docker compose exec api python -m webapp.api.mine_corrections --since 2026-10-01  # report over a window
```

Reports recurring OCR-confusion substitutions and names the tool didn't
recognize but DES confirmed were real - candidates to fold into
`pdf_extractor/parser.py` and `pdf_extractor/classify.py` deliberately (see
that script's docstring for why this is a candidate list, not an
auto-apply).

**Keeping what it learns.** The report alone forgets everything; these keep it
in the database (the `mined_patterns` table, created automatically the next
time the API or this script starts - no migration to run):

```bash
docker compose exec api python -m webapp.api.mine_corrections --save     # mine every correction, keep the result
docker compose exec api python -m webapp.api.mine_corrections --list     # what's waiting for a decision
docker compose exec api python -m webapp.api.mine_corrections --mark 12 accepted --by jane@akowe.example
docker compose exec api python -m webapp.api.mine_corrections --mark 13 rejected
```

- `--save` is safe to repeat: counts are recomputed from the corrections as
  they stand, not added to, and nobody's accept/reject decision is ever
  overwritten. It refuses `--job` / `--since`, because a partial count would
  overwrite the stored totals.
- `--list` shows undecided patterns, widest spread first: each pattern records
  how many different **booklets** it was seen in, and a pattern seen in three
  booklets outranks one seen thirty times in a single booklet (which is more
  likely one bad scan than a real confusion). `--min-booklets 3`, `--min-count`,
  `--kind` and `--status all` narrow it. A name token that later shows up in
  the vocabulary is closed out automatically as `in_vocabulary`.
- A booklet is a university plus a year, not an upload: the parts of a split
  booklet, or a re-upload, are one booklet (case and stray spaces in the
  university are ignored). The catch is that it goes by what was typed at
  upload - an undergraduate and a postgraduate booklet of the same university
  and year count as one, which understates the spread rather than inflating it.
- Accepting a pattern records the decision only. Editing `parser.py` /
  `classify.py` / the vocabulary is still a deliberate human change.
- Nothing runs `--save` by itself. To keep the list current, schedule it
  (a Railway cron job, or `cron` on the box) - weekly is plenty at this volume.
- What's stored is a character pair, a name token, a count and one token-level
  example ("OSUNNUB -> OSUNNUBI"), never a full name beyond what the `records`
  table already holds.

## Frontend: on Vercel

One environment variable once it exists: `NEXT_PUBLIC_API_URL` pointing at
the API's public HTTPS URL from step 1 above.
