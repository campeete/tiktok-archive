# Cloudflare R2 setup

R2 is an optional durable mirror for transcripts and DB backups. **Local is always primary**; R2 is a backup. If R2 vanishes, you lose nothing.

## Why R2

- Free tier: 10 GB of storage, no egress fees.
- 10 GB holds approximately 5 million transcript JSON files (avg 2 KB each).
- S3-compatible API → boto3 just works.

## What gets mirrored

| Path                                | Mirrored | Notes                              |
| ----------------------------------- | -------- | ---------------------------------- |
| `data/transcripts/{video_id}.json`  | yes      | Written on successful analyze      |
| `data/db-backups/tiktok-*.db`       | yes      | Written by `tiktok-archive backup-db` |
| `data/scratch/*.mp4`                | **no**   | Videos never leave the machine     |
| `data/tiktok.db`                    | **no**   | Use `backup-db` for snapshots      |

The split is intentional: video files would blow past the 10 GB free tier in one weekend, and uploading 10 MB then re-downloading per-video defeats the local-first principle.

## Setup

### 1. Create the bucket

1. Log in to Cloudflare Dashboard.
2. Navigate to **R2 Object Storage** → **Create bucket**.
3. Name it `tiktok-archive` (or whatever — match `.env`).
4. Region: pick the one closest to you (it doesn't really matter for free-tier).

### 2. Create an API token

1. R2 → **Manage R2 API Tokens** → **Create API Token**.
2. Permissions: **Object Read & Write**.
3. Specify bucket: select **only** the `tiktok-archive` bucket. **Don't grant account-wide access.**
4. TTL: set to 1 year max for hygiene.
5. Save the resulting `Access Key ID` and `Secret Access Key`. **Cloudflare will only show them once.**

### 3. Configure `.env`

```bash
TT_STORAGE_BACKEND=r2
R2_ACCOUNT_ID=<your-account-id>
R2_ACCESS_KEY_ID=<the-access-key-id>
R2_SECRET_ACCESS_KEY=<the-secret>
R2_BUCKET_NAME=tiktok-archive
```

`R2_ENDPOINT` is auto-derived from `R2_ACCOUNT_ID`. Only set it manually if you have a custom endpoint.

### 4. Install boto3

```bash
pip install -e ".[r2]"
```

### 5. Test

```bash
tiktok-archive setup-r2
```

Expected output: `✓ OK — bucket 'tiktok-archive' reachable`. The test puts a `.tiktok-archive-test` key, reads it back, and deletes it.

## Backups

Snapshot the SQLite DB to local + R2:

```bash
tiktok-archive backup-db
```

This writes:
- Local: `data/db-backups/tiktok-{YYYYMMDD-HHMMSS}.db`
- R2: `db-backups/tiktok-{YYYYMMDD-HHMMSS}.db`

Wire it into cron / launchd for nightly backups; see `scripts/backup-and-cleanup.sh`.

## Security hygiene

- **Rotate API tokens at least annually.** Set a calendar reminder when you create a token.
- **Never commit `.env`.** Verify with `git status` before every commit. The `.gitignore` has it covered, but assume the safety net might fail.
- **Bucket-scoped tokens only.** Account-wide tokens are a much bigger blast radius if leaked.
- **Watch the usage dashboard.** If something goes wrong (loop, wrong bucket, leaked token), you'll see it as a spike before you see it as a bill.

## Recovering from a leaked token

If you ever paste credentials into chat, screenshot, public repo, etc.:

1. R2 → **Manage R2 API Tokens** → revoke the leaked token.
2. Create a new token with the same scope.
3. Update `.env`.
4. `tiktok-archive setup-r2` to verify.
5. Treat anything in the bucket as potentially read by an attacker; for transcripts this is generally low-risk, but assess for your specific archive.

## Disabling R2 later

Simply set `TT_STORAGE_BACKEND=local` in `.env` and restart anything running. Local files remain. R2 contents are untouched (you'd need to delete them manually if you want to clean up).
