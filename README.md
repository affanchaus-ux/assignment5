# Assignment 5: End-to-End Mini Backup Workflow

`mini_backup.py` is a small Python tool that simulates a backup and restore workflow for a local directory of files (a "workload"). It supports full and incremental backups to **S3** or **NFS**, lists backups, restores any backup by walking its incremental chain back to the last full backup, verifies every restored file with MD5, and writes every action to a structured JSON log.

## Features

| Action | What it does |
|---|---|
| `define` | Scans a directory and records path, size, permissions, owner/group (and UID/GID) and MD5 of every file into `workload_metadata.json` |
| `backup --full` | Uploads all files to `backups/<name>/<timestamp>/full/` on S3 or NFS and writes `manifest.json` |
| `backup --incremental` | Compares current MD5s with the last backup on the same target, uploads only changed files to `backups/<name>/<timestamp>/incremental/`, and writes a delta manifest that points to the previous run |
| `list` | Shows all backups of a workload with timestamp, type, target, size and file count |
| `restore` | Identifies full/incremental, walks the backing chain to the last full backup, replays it, restores directory structure, permissions and ownership, and reports MD5 PASS/FAIL per file |

Every run is recorded in `backup_catalog.json`, and every action is logged as one JSON object per line in `mini_backup.log`.

## Requirements

- Linux (tested on Ubuntu, AWS EC2 VM)
- Python 3 with `venv`
- Docker (for the local S3-compatible server)
- `nfs-kernel-server` (for the NFS target)
- Python packages: see `requirements.txt` (`boto3` and its dependencies)

## Setup

### 1. S3 target (SeaweedFS, S3-compatible, on port 9000)

MinIO images are no longer freely pullable, so SeaweedFS is used as a local S3-compatible server.

```bash
mkdir -p ~/seaweed-data
docker run -d --name seaweed-backup -p 9000:9000 -v ~/seaweed-data:/data chrislusf/seaweedfs mini -dir=/data -s3.port=9000
```

Credentials used by the script (defaults, can be overridden with `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`):

- Access key: `minioadmin`
- Secret key: `minioadmin`

The script creates the bucket `mini-backups` automatically on the first S3 backup.

If the VM is restarted, start the container again:

```bash
docker start seaweed-backup
```

### 2. NFS target (real NFS server on the same VM)

```bash
sudo apt install nfs-kernel-server -y
sudo mkdir -p /srv/nfs/backups
sudo chown ubuntu:ubuntu /srv/nfs/backups
```

Add this line to `/etc/exports` (`sudo nano /etc/exports`):

```
/srv/nfs/backups 127.0.0.1(rw,sync,no_subtree_check)
```

Apply the export and mount it:

```bash
sudo exportfs -ra
sudo mkdir -p /mnt/nfs-backup
sudo mount -t nfs 127.0.0.1:/srv/nfs/backups /mnt/nfs-backup
df -h /mnt/nfs-backup
```

`df` must show `127.0.0.1:/srv/nfs/backups` as the filesystem. The mount is not permanent: after a VM restart, run the `mount` command again. The script refuses to back up or restore if `/mnt/nfs-backup` is not a mount point, so data never goes to the local disk by mistake.

### 3. Python environment

```bash
mkdir -p ~/assignment5-mini-backup
cd ~/assignment5-mini-backup
python3 -m venv minibackupenv
source minibackupenv/bin/activate
pip install -r requirements.txt
```

## End-to-end walkthrough

All commands are run from `~/assignment5-mini-backup` with the virtual environment active. The outputs below are from the actual test run.

### Step 0: Create a test workload

```bash
mkdir -p workload/data
echo "app_name=demo-workload" > workload/app.conf
echo "record-1" > workload/data/records.txt
echo "echo starting demo app" > workload/start.sh
chmod 755 workload/start.sh
ls -lR workload
```

The workload has 3 files (55 bytes), one inside a subfolder and one with different permissions (`755`), so that directory structure and permission restore can be tested.

### Step 1: Define the workload

```bash
python3 mini_backup.py --action define --workload-path workload --name demo
```

Result: `file_count: 3`, `total_size: 55`, and for each file its path (`data/records.txt` is stored as a relative path), size, permissions (`0o664` / `0o755`), owner `ubuntu:ubuntu` (UID/GID 1000) and MD5, saved in `workload_metadata.json`.

### Step 2: Full backup to NFS

```bash
python3 mini_backup.py --action backup --name demo --target nfs --full
```

Result: timestamp `20261008T152717Z`, type `full`, 3 files, 55 bytes, manifest at `/mnt/nfs-backup/backups/demo/20261008T152717Z/manifest.json`.

Verify on the NFS server side:

```bash
find /srv/nfs/backups -type f
```

```
/srv/nfs/backups/backups/demo/20261008T152717Z/manifest.json
/srv/nfs/backups/backups/demo/20261008T152717Z/full/data/records.txt
/srv/nfs/backups/backups/demo/20261008T152717Z/full/start.sh
/srv/nfs/backups/backups/demo/20261008T152717Z/full/app.conf
```

### Step 3: Change one file and take an incremental backup

```bash
echo "record-2" >> workload/data/records.txt
python3 mini_backup.py --action backup --name demo --target nfs --incremental
```

Result: timestamp `20261008T152949Z`, type `incremental`, `parent_timestamp: 20261008T152717Z`, only **1 file** (`data/records.txt`, 18 bytes) uploaded to `.../incremental/`.

### Step 4: Full backup to S3

```bash
python3 mini_backup.py --action backup --name demo --target s3 --full
```

Result: timestamp `20261008T153212Z`, 3 files, 64 bytes, manifest at `s3://mini-backups/backups/demo/20261008T153212Z/manifest.json`.

### Step 5: List backups

```bash
python3 mini_backup.py --action list --name demo
```

```
TIMESTAMP          TYPE         TARGET   SIZE(B)  FILES
20261008T152717Z   full         nfs           55      3
20261008T152949Z   incremental  nfs           18      1
20261008T153212Z   full         s3            64      3
```

The table goes to stderr and the JSON result (`count: 3`, with `parent_timestamp` for each backup) goes to stdout.

### Step 6: Restore the incremental backup

```bash
python3 mini_backup.py --action restore --name demo --timestamp 20261008T152949Z --target-path restored-nfs
```

Result:

- `chain: ["20261008T152717Z", "20261008T152949Z"]`: the incremental was followed back to its full backup, and the chain was replayed full first, then the incremental on top
- 3 files restored into `restored-nfs/`, including the `data/` subfolder
- `start.sh` restored with `0o755`, all files with owner `ubuntu:ubuntu`
- MD5 `PASS` for all 3 files (`data/records.txt` has the incremental version, MD5 `6d9bd1c5...`)
- `overall_status: PASS`

Independent verification:

```bash
diff -r workload restored-nfs
ls -lR restored-nfs
```

`diff` prints nothing (identical content), and `ls -lR` shows the same permissions, owner and structure as the original workload.

### Step 7: Check the structured log

```bash
cat mini_backup.log
python3 -c "import json; lines = open('mini_backup.log').read().splitlines(); [json.loads(l) for l in lines]; print(len(lines), 'lines, all valid JSON')"
```

Result: `29 lines, all valid JSON`. Events include `workload_defined`, `backup_started`, `file_uploaded`, `backup_completed`, `bucket_created`, `backups_listed`, `restore_started`, `restore_chain_resolved`, `file_restored`, `checksum_verified`, `restore_completed` and `error`.

## Backup layout on the target

```
backups/
└── demo/
    ├── 20261008T152717Z/
    │   ├── manifest.json
    │   └── full/
    │       ├── app.conf
    │       ├── start.sh
    │       └── data/records.txt
    └── 20261008T152949Z/
        ├── manifest.json        (type: incremental, parent_manifest -> 20261008T152717Z)
        └── incremental/
            └── data/records.txt
```

Each `manifest.json` contains: type, target, `parent_timestamp` and `parent_manifest` (pointer to the previous run, `null` for full), the files uploaded in this run, `deleted_files`, and a `snapshot` of the full workload state (path, size, permissions, owner, UID/GID, MD5) at backup time.

## Files

| File | Created by | Purpose |
|---|---|---|
| `mini_backup.py` | deliverable | The tool |
| `requirements.txt` | deliverable | Python dependencies |
| `README.md` | deliverable | This walkthrough |
| `workload_metadata.json` | `define` | File details of each defined workload |
| `backup_catalog.json` | `backup` | Record of all backup runs |
| `mini_backup.log` | every action | Structured JSON log (one event per line) |

## Notes

- Incremental backups compare against the latest backup of the same workload on the **same target**, so NFS and S3 have separate chains.
- Restore refuses a non-empty `--target-path` to avoid overwriting existing files.
- Ownership is restored with the saved UID/GID. A normal user can only set ownership to itself; if the original owner is a different user, the file is restored and reported as `ownership: skipped (needs root)`.
- Errors (missing NFS mount, unreachable S3, missing arguments) are returned as JSON and also written to the log.
