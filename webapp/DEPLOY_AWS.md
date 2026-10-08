# Deploying Agent Nyocha to AWS

For whoever is deploying it. The target is **one EC2 instance running containers, RDS Postgres for the database, and S3 for files**. It is sized for the expected load (well under 200 booklets a month, about 15 reviewers) and is simple enough to rebuild from this document alone.

> **This system holds real students' academic records.** Keep the bucket private, the database private, encryption on, and keep this repository, the `.env` file and any backups private. Nothing in the repository contains student data and the images built from it must not either (see step 5).

## 1. What runs where

```
 reviewer's browser
        │ HTTPS
        ▼
   ┌─────────────────────── EC2 instance (Docker) ───────────────────────┐
   │  caddy  ──►  web   (Next.js review screen, port 3000)               │
   │         └──► api   (FastAPI, port 8000)  ──► RDS Postgres           │
   │              worker (OCR; polls the database for queued jobs) ──►   │
   └──────────────────────────────────────────────────────────────┬──────┘
                                                                  ▼
                                                          S3 bucket (private)
```

| Part | What it is | Why it is its own container |
|---|---|---|
| `api` | FastAPI. Login, upload, review screen data, file and zip downloads. | Must stay responsive while OCR runs. |
| `worker` | The extraction engine: Tesseract OCR, OpenCV, PyMuPDF. Takes queued jobs from the database, writes results back. Takes minutes per booklet and is CPU heavy. | Needs the Tesseract program installed, and its load must not slow the API. |
| `web` | Next.js 14 review screen. | Separate build; the API's address is fixed at build time. |
| `caddy` | HTTPS and routing; gets and renews certificates by itself. | Optional: replace it with an Application Load Balancer if you prefer. |

`api` and `worker` are the same image with different commands (`webapp/api/Dockerfile`).

## 2. Read this first: what has and hasn't been verified

Be clear-eyed about this. The application was developed and tested on a Windows laptop with SQLite and local-disk files.

**Verified:** all four test suites pass (`python -m pdf_extractor.test_units`, `webapp.api.test_mine_corrections`, `webapp.api.test_page_export`, `webapp.api.test_deploy`); the review screen builds for production; the code was exercised end to end on real booklet pages.

**Not verified - these are your first jobs:**
1. **Postgres has never been run.** The code avoids Postgres-only and SQLite-only features and the schema upgrade runs under a Postgres advisory lock, but it has not been executed on Postgres. Step 7 (`check_deploy`) and the smoke test in step 9 are how you find out.
2. **The Docker images have never been built.** The Dockerfiles and compose files are written carefully but unbuilt; expect to fix a typo or version if the first build complains.
3. **Nothing has run on AWS.** The IAM, networking and S3 settings below are standard practice, not tested here.

If a step fails, fix it and carry on; if something in this document is wrong, correct the document.

## 3. Decisions to make before starting

| Decision | Notes |
|---|---|
| **AWS region** | Student records: check any data-residency or data-protection requirements before choosing. Everything below goes in the one region. |
| **Domain** | Two hostnames pointing at the instance, e.g. `review.<domain>` (the screen) and `api.<domain>` (the API). |
| **Who may reach it** | The simplest safe option: if reviewers work from fixed offices, restrict ports 80/443 in the security group to those IP ranges. See "Known gaps". |
| **Backups** | RDS automated backups plus S3 versioning (below). Decide the retention period. |

## 4. Create the AWS resources

**S3 bucket** (one, private)
- Block **all** public access: on. ACLs disabled. Default encryption: SSE-S3 (or a KMS key if you require it).
- **Versioning: on** (an overwritten or deleted page image can then be recovered).
- No bucket policy is needed; access is through the instance role below.

**IAM role for the instance** (attach as the EC2 instance profile) - least privilege for the bucket, nothing else:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
     "Resource": "arn:aws:s3:::YOUR-BUCKET/*"},
    {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": "arn:aws:s3:::YOUR-BUCKET"}
  ]
}
```
Also attach `AmazonSSMManagedInstanceCore` if you will use Session Manager instead of SSH (recommended).

**RDS PostgreSQL 16**
- Single instance is fine to start (`db.t4g.small` or `db.t3.small` is a reasonable guess; watch it). 50 GB gp3 with storage autoscaling. **Encryption: on. Publicly accessible: no. Automated backups: on** (7 days or your policy). Multi-AZ if you want automatic failover.
- Database name `extraction_review`, user `akowe`. **Use a password of letters and digits only** - it is placed inside a URL.
- Security group: allow port **5432 from the EC2 instance's security group only**.

**EC2 instance**
- Ubuntu Server 24.04 LTS, `c6i.xlarge` (4 vCPU, 8 GB) as a starting guess - extraction is CPU-bound and a single upload is read fully into memory, so watch memory with the largest booklet PDFs. 100 GB gp3 **encrypted** disk (temporary page images for a large booklet run to hundreds of MB).
- Attach the IAM role above.
- **Instance metadata: IMDSv2 required, hop limit 2.** (With the default hop limit of 1, code running inside a container cannot reach the instance role and S3 access fails with "Unable to locate credentials".) CLI: `aws ec2 modify-instance-metadata-options --instance-id i-... --http-tokens required --http-put-response-hop-limit 2`.
- Security group: inbound **80 and 443** (from the internet, or the reviewers' ranges); **no inbound 22** if you use Session Manager, otherwise 22 from your own address only. Outbound open.
- Attach an **Elastic IP**, and create DNS **A records** for both hostnames pointing at it.

## 5. Install and build

On the instance:
```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker
sudo usermod -aG docker $USER      # log out and back in

git clone <the private repository URL> app && cd app/webapp
cp .env.aws.example .env && chmod 600 .env
```
Edit `.env` (every value is described in the file): the two hostnames, region, bucket name, the RDS address as `DATABASE_URL` (keep `?sslmode=require`), and a signing secret from `openssl rand -hex 32`.

Docker log rotation (otherwise container logs can fill the disk) - create `/etc/docker/daemon.json`, then `sudo systemctl restart docker`:
```json
{"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "3"}}
```

Build:
```bash
docker compose -f docker-compose.aws.yml build
```
The first build takes several minutes. **Then confirm no data is inside the image** (it must print nothing before the file list; the repository's `.dockerignore` is what guarantees this):
```bash
docker compose -f docker-compose.aws.yml run --rm --no-deps --entrypoint sh api -c \
  'find /app -type f \( -name "*.pdf" -o -name "*.db" -o -name "*.csv" \) | head; echo ---; ls /app/pdf_extractor | head -30'
```

## 6. Start

```bash
docker compose -f docker-compose.aws.yml up -d
docker compose -f docker-compose.aws.yml ps          # api, worker, web, caddy all "running"; api "healthy"
docker compose -f docker-compose.aws.yml logs --tail 50 api worker
```
- The API and worker **refuse to start** if the settings are unsafe (missing or short `JWT_SECRET`, `CORS_ALLOW_ORIGINS` missing or `*`, no database address) and print which. That is deliberate.
- On first start they create the tables in RDS. They may start at the same time; this is handled with a database lock.

## 7. Verify the deployment

```bash
docker compose -f docker-compose.aws.yml exec api python -m webapp.api.check_deploy
```
It checks the settings, the database (reachable, tables, upgrades, a rolled-back write), S3 (writes, reads and deletes a small test file), Tesseract, and PDF rendering, and prints `OK`, `WARN` or `FAIL` for each. **Everything must be OK**, apart from a `WARN` that no admin user exists yet. It leaves nothing behind (on a versioned bucket, a delete marker for its one test file). Re-run it after any change to `.env`.

Typical failures: *Unable to locate credentials* → IMDS hop limit (step 4); *AccessDenied* → the IAM policy or bucket name; *could not connect to server* → the RDS security group or the address in `DATABASE_URL`; *no pg_hba.conf entry … no encryption* → keep `?sslmode=require`.

## 8. Create the first accounts

There is no self-sign-up. Accounts are created from the command line (it prompts for the password, minimum 8 characters - use long ones):
```bash
docker compose -f docker-compose.aws.yml exec api python -m webapp.api.seed_users add jane@akowe.example "Jane Doe" --admin
docker compose -f docker-compose.aws.yml exec api python -m webapp.api.seed_users add des1@akowe.example "A Reviewer"
```
Running `add` again for an existing email **resets** that person's password. There is no admin screen for this yet - this is the way to add people and reset passwords.

## 9. Smoke test (do this before telling anyone it's live)

1. Open `https://<WEB_HOST>` and log in.
2. Upload a small PDF booklet (a few pages is enough) with a university and year.
3. Watch the job go `queued` → `processing` → `ready` (a few minutes). In `logs worker` you will see `processing job N` / `finished job N`.
4. Open the job: rows appear; flagged rows show a snapshot image. In the S3 console, confirm files appeared under `jobs/<id>/`.
5. If the job shows pages needing a page number, set them (a page's number is set once for all its rows), then confirm **Download page images** produces a zip of PNGs named like `unilag-2019-102.png`.
6. Kill the worker mid-job to prove recovery: upload another booklet, `docker compose -f docker-compose.aws.yml restart worker` while it is `processing`; within about 10 minutes the job goes back to `queued` and completes (after 3 failed attempts it is marked failed with an explanation instead).
7. Open `https://<API_HOST>/health` → `{"ok":true}`; `https://<API_HOST>/docs` → not found (docs are off in production).

## 10. Day-to-day operations

| Task | How |
|---|---|
| **Update the app** | `git pull && docker compose -f docker-compose.aws.yml build && docker compose -f docker-compose.aws.yml up -d`. New database columns are added automatically at start (additive only). Run `check_deploy` after. |
| **Roll back** | `git checkout <previous tag or commit>`, rebuild, `up -d`. The database changes are additive, so older code runs against the newer schema. |
| **Logs** | `docker compose -f docker-compose.aws.yml logs -f worker` (or `api`). Consider shipping them to CloudWatch (Docker `awslogs` driver; add `logs:CreateLogStream`/`PutLogEvents` to the role). |
| **More throughput** | `up -d --scale worker=2`. Safe: a job is claimed by one worker only. Mind the instance's CPU and memory. |
| **A job is stuck "processing"** | Leave it. A job whose worker has been silent for 10 minutes is requeued automatically (heartbeat every 30 s); after 3 attempts it is marked failed with a message. |
| **A job failed** | The job page shows the error. If "stopped N times without finishing", the booklet is probably too large for the instance: use a bigger one. |
| **Add or reset a user** | Step 8. |
| **Check settings** | `exec api python -m webapp.api.check_deploy`. |
| **Restart** | `docker compose -f docker-compose.aws.yml restart worker` (or `api`, `web`). |

**Monitor** (CloudWatch alarms or similar): EC2 CPU and disk usage (install the CloudWatch agent for disk), RDS free storage and CPU, and an uptime check on `https://<API_HOST>/health`.

**Backups:** RDS automated backups cover the database; S3 versioning covers the files. **Restore a backup once, to a scratch RDS instance, before relying on it.**

## 11. Configuration reference

All set through `.env` (see `docker-compose.aws.yml` for how they reach the containers). Only the first group is needed.

| Setting | Meaning |
|---|---|
| `API_HOST`, `WEB_HOST` | Hostnames for the API and review screen. The review screen is built with the API's address, so rebuild `web` if `API_HOST` changes. |
| `AWS_REGION`, `S3_BUCKET` | The bucket. S3 access is by the instance role - **do not put AWS keys in `.env`**. |
| `DATABASE_URL` | `postgresql+psycopg2://akowe:PASSWORD@HOST:5432/extraction_review?sslmode=require` |
| `JWT_SECRET` | Signs login sessions. 32+ random characters. Changing it logs everyone out. |

Optional (defaults are sensible; set in the `x-app-env` block of the compose file): `JWT_EXPIRE_HOURS` (12), `WORKER_POLL_SECONDS` (5), `WORKER_HEARTBEAT_SECONDS` (30), `STALE_JOB_SECONDS` (600), `MAX_JOB_ATTEMPTS` (3), `EXTRACTION_ZOOM` (2.0, OCR quality), `PAGE_IMAGE_ZOOM` (2.0, exported page image size), `PAGE_IMAGE_NAME_PATTERN` and `PAGE_IMAGE_SLUGS` (page image file names; see `webapp/README.md`). `STORAGE_BACKEND` is `s3` here; `local` is for laptops only.

## 12. Known gaps (be aware, decide, or ask for them to be fixed)

- **No login rate limit or lockout.** Anyone who can reach the login page can guess passwords. Mitigations: long passwords; restrict the security group to the reviewers' IP ranges; or, with an ALB, an AWS WAF rate-limit rule on `/auth/login`. This should be fixed in the code if the system is open to the whole internet.
- **Download and image links carry the login token in the web address** (so `<img>` tags and download links work). It then appears in access logs - **do not enable ALB or proxy access logs that record query strings**, or treat them as sensitive. Keep `JWT_EXPIRE_HOURS` short.
- **Accounts and passwords are command-line only**; there is no admin screen, no email, no self-service password reset.
- **No migration tool.** Added columns are applied automatically; any change beyond adding columns would be a manual SQL step. (Fine now; matters later.)
- **One API process, no autoscaling.** Appropriate for this volume.
- **Uploads are read into memory** by the API. A very large PDF needs the memory to match.
- **The reviewers' mining/correction report** (`python -m webapp.api.mine_corrections ...`, see `webapp/README.md`) is run by hand; nothing schedules it.

## 13. Alternative: ECS Fargate instead of a single instance

Use the same image for two services (`api` default command; `worker` command `python -m webapp.api.worker`), RDS, S3, an ALB with an ACM certificate in front of `api` (target group health check path `/health`, **raise the idle timeout above the default 60 s** for large uploads and downloads) and `web`, secrets in Secrets Manager, task role with the S3 policy above, logs to CloudWatch. Give the worker task at least 4 vCPU / 8 GB. Do not use API Gateway or Lambda - uploads and the page-image zip are too large and slow for them. The settings and checks above apply unchanged.
