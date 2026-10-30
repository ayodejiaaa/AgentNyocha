# Agent Nyocha - review webapp

Two halves in one image family, deployed together:

- **`webapp/api`** - the HTTP API **and** the worker (two containers from one image,
  `webapp/api/Dockerfile`). The worker needs Tesseract and several minutes of CPU per
  booklet, which is why this is a container on a real server, not a serverless function.
- **`webapp/frontend`** - the Next.js review screen (`webapp/frontend/Dockerfile`).

**To deploy to AWS, follow [`DEPLOY_AWS.md`](DEPLOY_AWS.md)** (EC2 + RDS Postgres + S3, with
HTTPS; includes the resources to create, every step, a self-check command and the known
gaps). `docker-compose.aws.yml` is that deployment; `docker-compose.yml` runs everything
including a Postgres container on one box, for a trial. Settings are described in
`.env.aws.example` / `.env.example`.

The API and worker **refuse to start** with unsafe settings (no `JWT_SECRET`, wildcard or
missing `CORS_ALLOW_ORIGINS`, no database address) - see `api/config.py`. Run
`python -m webapp.api.check_deploy` (inside the API container) to check a deployment.

Tests (none touch real data or the real database):

```bash
python -m pdf_extractor.test_units
python -m webapp.api.test_mine_corrections
python -m webapp.api.test_page_export
python -m webapp.api.test_deploy
python -m webapp.api.test_progress
python -m webapp.api.test_remove_rows
```

Local development needs none of the above: with nothing set it uses SQLite and a local
folder, and the review screen on localhost:3000 may call the API.

## Creating DES accounts

No self-serve signup (see `api/security.py`) - an admin runs this once per
person:

```bash
docker compose -f docker-compose.aws.yml exec api python -m webapp.api.seed_users add jane@akowe.example "Jane Doe"
```

## Page images, for building reports

When a job is processed, every page of the booklet that has names on it is saved
as an image (the whole page), along with a second image of just its top and
bottom edges - where the printed page number is.

- **Checking a page number.** A row flagged `page_number_uncertain` shows the
  edge image at the top of its right-hand column, above the name snapshot (the
  snapshot is a crop around the name and never contained the page number). Any
  other row has a "Show the page number" link.
- **Page numbers must all be resolved before the download.** The job page shows
  *Download page images* greyed out, with "N pages need a page number first",
  until every page has exactly one confirmed number. "Show which" lists the pages
  in the way, each with its edge image, an input, and **Set for all N rows** - a
  page's number is a fact about the page, so it is set once for every row on it,
  not row by row. Three things hold the download back: a page with no number, two
  pages with the same number (one is wrong), and rows on a page that disagree
  about its number (some were edited, some not).
- **The download** is one PNG per page, named like Akowe's existing page image
  files - `covenant-university-2022-28.png`, `unibadan-2016-152.png`,
  `futa-2024-100.png` - in a zip with a `manifest.csv` (file, page number, source
  file, physical page, rows). PNG only.
  (`GET /jobs/{id}/page-images`; refused with a 409 and the reason while anything
  is unresolved, and `GET /jobs/{id}/page-images/status` says what is in the way.)
- **A scan that shows two page numbers is held for a person.** A scan often runs
  into the corner of the neighbouring page, so a page can show its own number and
  the one beside it. The tool reads both bottom corners of every page, learns
  which corner this booklet keeps its numbers in (separately for odd and even
  pages) from the readings that fit the booklet's sequence, and holds a page back
  when (a) the two corners show different numbers, or (b) the only number it can
  read is in the other corner (so it is the neighbour's and the page's own wasn't
  read). Such a page is never filed under a number by itself - not even one the
  sequence would fill in. It is listed under "pages that need a page number" with
  what it showed ("33 in the left corner and 34 in the right corner") and the
  number the booklet's sequence points to, which is put in the box for the
  reviewer to check against the image and set. A number printed in a corner on
  many pages (a convocation ordinal) is ignored. Limits: it reads the bottom
  corners only, and a number it cannot read (a bold digit on a coloured tab
  often defeats OCR) is simply not seen - so the flag fires on what it saw, and
  the sequence check still catches the rest.
- **A page is filed under the number on its rows**, not what the tool first read,
  so correcting it is what the download uses. A page image that can't be read from
  storage fails the download and names the page - it is never left out silently.
- **Naming.** `{slug}-{year}-{page}`: the *slug* is the university's short name
  from a table in `webapp/api/page_export.py` (`UNIVERSITY_SLUGS`) - it can't be
  derived, because the existing files call the University of Ibadan `unibadan`,
  not `university-of-ibadan`. A postgraduate booklet - "PG", "PGS" or
  "Postgraduate" in the university typed at upload, any casing - gets `-pgs`
  after the short name (`unilag-pgs-2019-102`, beside the undergraduate
  `unilag-2019-102`), and "UG"/"Undergraduate" is the unmarked case; the same
  rule covers every university (OOU confirmed to follow it). LASU, OAU, OOU and
  UNILORIN have no sample to confirm their short names and use their lower-case
  names; check them. Add or correct any
  with `PAGE_IMAGE_SLUGS` (JSON, e.g. `{"UNILAG": "unilag-ug"}`), matched ignoring
  case, spaces and punctuation. The *year* is the first four-digit year in what
  was typed at upload. `PAGE_IMAGE_NAME_PATTERN` changes the whole shape
  (`{slug}`, `{year}`, `{page}`, `{university}`; a bad pattern is refused rather
  than quietly producing wrongly named files).
- **Image size** is `PAGE_IMAGE_ZOOM` (default `2.0`, about 1,200 x 1,700 px per
  page; raise it for sharper images at a larger size).
- A page number is 1 to 3 digits (booklets never run to a thousand pages), so a
  printed year can never be entered as one.
- Jobs processed before this existed have no page images (their rows say so, and
  the download explains it). Upload the booklet again to get them - note that
  this creates a new job, so corrections already made on the old one are not
  carried over.
- The database is upgraded automatically on start (a new `pages` table and a
  `page_id` column on `records`); nothing to run.

## Feeding corrections back into the tool

Once DES have been correcting flagged rows for a while:

```bash
docker compose -f docker-compose.aws.yml exec api python -m webapp.api.mine_corrections                     # report only
docker compose -f docker-compose.aws.yml exec api python -m webapp.api.mine_corrections --since 2026-10-01  # report over a window
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
docker compose -f docker-compose.aws.yml exec api python -m webapp.api.mine_corrections --save     # mine every correction, keep the result
docker compose -f docker-compose.aws.yml exec api python -m webapp.api.mine_corrections --list     # what's waiting for a decision
docker compose -f docker-compose.aws.yml exec api python -m webapp.api.mine_corrections --mark 12 accepted --by jane@akowe.example
docker compose -f docker-compose.aws.yml exec api python -m webapp.api.mine_corrections --mark 13 rejected
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
  (`cron` on the server, or an EventBridge scheduled task) - weekly is plenty at this volume.
- What's stored is a character pair, a name token, a count and one token-level
  example ("OSUNNUB -> OSUNNUBI"), never a full name beyond what the `records`
  table already holds.
