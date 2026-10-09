import argparse
import datetime
import grp
import hashlib
import json
import os
import pwd
import shutil
import stat
import sys

METADATA_FILE = "workload_metadata.json"
CATALOG_FILE = "backup_catalog.json"
LOG_FILE = "mini_backup.log"

NFS_ROOT = "/mnt/nfs-backup"
S3_ENDPOINT = "http://localhost:9000"
S3_BUCKET = "mini-backups"


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def log_event(event, **details):
    entry = {"timestamp": now_iso(), "event": event}
    entry.update(details)
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def md5_of_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_workload(workload_path):
    files = []
    for root, dirs, filenames in os.walk(workload_path):
        dirs.sort()
        for name in sorted(filenames):
            full_path = os.path.join(root, name)
            st = os.stat(full_path)
            files.append({
                "path": os.path.relpath(full_path, workload_path),
                "size": st.st_size,
                "permissions": oct(stat.S_IMODE(st.st_mode)),
                "owner": pwd.getpwuid(st.st_uid).pw_name,
                "group": grp.getgrgid(st.st_gid).gr_name,
                "uid": st.st_uid,
                "gid": st.st_gid,
                "md5": md5_of_file(full_path),
            })
    return files


# ---------------- Backup targets ----------------

class NFSTarget:
    def __init__(self):
        self.root = NFS_ROOT
        if not os.path.ismount(self.root):
            raise RuntimeError(f"NFS share is not mounted at {self.root}")

    def location(self, key):
        return os.path.join(self.root, key)

    def put_file(self, local_path, key, md5):
        dest = self.location(key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(local_path, dest)

    def put_json(self, key, data):
        dest = self.location(key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w") as f:
            json.dump(data, f, indent=2)

    def get_file(self, key, local_path):
        shutil.copyfile(self.location(key), local_path)

    def get_json(self, key):
        with open(self.location(key)) as f:
            return json.load(f)


class S3Target:
    def __init__(self):
        import boto3
        from botocore.exceptions import ClientError
        self.client = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "minioadmin"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "minioadmin"),
            region_name="us-east-1",
        )
        try:
            self.client.head_bucket(Bucket=S3_BUCKET)
        except ClientError:
            self.client.create_bucket(Bucket=S3_BUCKET)
            log_event("bucket_created", bucket=S3_BUCKET)

    def location(self, key):
        return f"s3://{S3_BUCKET}/{key}"

    def put_file(self, local_path, key, md5):
        self.client.upload_file(local_path, S3_BUCKET, key,
                                ExtraArgs={"Metadata": {"md5": md5}})

    def put_json(self, key, data):
        self.client.put_object(Bucket=S3_BUCKET, Key=key,
                               Body=json.dumps(data, indent=2).encode(),
                               ContentType="application/json")

    def get_file(self, key, local_path):
        self.client.download_file(S3_BUCKET, key, local_path)

    def get_json(self, key):
        obj = self.client.get_object(Bucket=S3_BUCKET, Key=key)
        return json.loads(obj["Body"].read())


def get_target(name):
    return S3Target() if name == "s3" else NFSTarget()


# ---------------- Task 1: define ----------------

def define_action(workload_path, name):
    if not os.path.isdir(workload_path):
        return {"action": "define", "error": f"workload path not found: {workload_path}"}
    workload_path = os.path.abspath(workload_path)
    files = scan_workload(workload_path)
    total_size = sum(f["size"] for f in files)
    metadata = load_json(METADATA_FILE, {})
    metadata[name] = {
        "name": name,
        "workload_path": workload_path,
        "defined_at": now_iso(),
        "file_count": len(files),
        "total_size": total_size,
        "files": files,
    }
    save_json(METADATA_FILE, metadata)
    log_event("workload_defined", name=name, workload_path=workload_path,
              file_count=len(files), total_size=total_size)
    return {
        "action": "define",
        "name": name,
        "workload_path": workload_path,
        "file_count": len(files),
        "total_size": total_size,
        "metadata_file": METADATA_FILE,
        "files": files,
    }


# ---------------- Task 2: backup ----------------

def backup_action(name, target_name, mode):
    metadata = load_json(METADATA_FILE, {})
    if name not in metadata:
        return {"action": "backup", "error": f"workload '{name}' not defined, run --action define first"}
    workload_path = metadata[name]["workload_path"]
    if not os.path.isdir(workload_path):
        return {"action": "backup", "error": f"workload path not found: {workload_path}"}

    catalog = load_json(CATALOG_FILE, [])
    previous = None
    if mode == "incremental":
        runs = [r for r in catalog if r["name"] == name and r["target"] == target_name]
        if not runs:
            return {"action": "backup",
                    "error": f"no previous backup of '{name}' on {target_name}, run a --full backup first"}
        previous = max(runs, key=lambda r: r["timestamp"])

    target = get_target(target_name)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_prefix = f"backups/{name}/{timestamp}"
    data_prefix = f"{run_prefix}/{mode}"

    log_event("backup_started", name=name, target=target_name, type=mode,
              backup_timestamp=timestamp, parent=previous["timestamp"] if previous else None)

    current_files = scan_workload(workload_path)

    if mode == "full":
        to_upload = current_files
        deleted = []
    else:
        prev_snapshot = {f["path"]: f["md5"] for f in previous["snapshot"]}
        to_upload = [f for f in current_files if prev_snapshot.get(f["path"]) != f["md5"]]
        current_paths = {f["path"] for f in current_files}
        deleted = sorted(p for p in prev_snapshot if p not in current_paths)

    for f in to_upload:
        key = f"{data_prefix}/{f['path']}"
        target.put_file(os.path.join(workload_path, f["path"]), key, f["md5"])
        log_event("file_uploaded", name=name, backup_timestamp=timestamp,
                  file=f["path"], size=f["size"], md5=f["md5"],
                  location=target.location(key))

    uploaded_size = sum(f["size"] for f in to_upload)
    manifest = {
        "name": name,
        "timestamp": timestamp,
        "type": mode,
        "target": target_name,
        "workload_path": workload_path,
        "parent_timestamp": previous["timestamp"] if previous else None,
        "parent_manifest": f"backups/{name}/{previous['timestamp']}/manifest.json" if previous else None,
        "file_count": len(to_upload),
        "size": uploaded_size,
        "files": to_upload,
        "deleted_files": deleted,
        "snapshot": current_files,
    }
    manifest_key = f"{run_prefix}/manifest.json"
    target.put_json(manifest_key, manifest)

    catalog.append({
        "name": name,
        "timestamp": timestamp,
        "type": mode,
        "target": target_name,
        "parent_timestamp": manifest["parent_timestamp"],
        "file_count": len(to_upload),
        "size": uploaded_size,
        "deleted_files": deleted,
        "manifest": target.location(manifest_key),
        "snapshot": current_files,
    })
    save_json(CATALOG_FILE, catalog)

    log_event("backup_completed", name=name, target=target_name, type=mode,
              backup_timestamp=timestamp, file_count=len(to_upload), size=uploaded_size)

    return {
        "action": "backup",
        "name": name,
        "type": mode,
        "target": target_name,
        "timestamp": timestamp,
        "parent_timestamp": manifest["parent_timestamp"],
        "file_count": len(to_upload),
        "size": uploaded_size,
        "uploaded_files": [f["path"] for f in to_upload],
        "deleted_files": deleted,
        "manifest": target.location(manifest_key),
    }


# ---------------- Task 3: list ----------------

def list_action(name):
    catalog = load_json(CATALOG_FILE, [])
    runs = sorted((r for r in catalog if r["name"] == name), key=lambda r: r["timestamp"])
    backups = [{
        "timestamp": r["timestamp"],
        "type": r["type"],
        "target": r["target"],
        "size": r["size"],
        "file_count": r["file_count"],
        "parent_timestamp": r["parent_timestamp"],
    } for r in runs]

    print(f"{'TIMESTAMP':<18} {'TYPE':<12} {'TARGET':<7} {'SIZE(B)':>8} {'FILES':>6}", file=sys.stderr)
    for b in backups:
        print(f"{b['timestamp']:<18} {b['type']:<12} {b['target']:<7} {b['size']:>8} {b['file_count']:>6}",
              file=sys.stderr)

    log_event("backups_listed", name=name, count=len(backups))
    return {"action": "list", "name": name, "count": len(backups), "backups": backups}


# ---------------- Task 4: restore ----------------

def restore_action(name, timestamp, target_path):
    catalog = load_json(CATALOG_FILE, [])
    run = next((r for r in catalog if r["name"] == name and r["timestamp"] == timestamp), None)
    if not run:
        return {"action": "restore", "error": f"no backup of '{name}' with timestamp {timestamp}"}
    if os.path.exists(target_path) and os.listdir(target_path):
        return {"action": "restore", "error": f"target path {target_path} is not empty"}

    target = get_target(run["target"])
    log_event("restore_started", name=name, backup_timestamp=timestamp,
              type=run["type"], target=run["target"], target_path=target_path)

    # walk the backing chain back to the last full backup
    chain = []
    manifest = target.get_json(f"backups/{name}/{timestamp}/manifest.json")
    chain.append(manifest)
    while manifest["type"] == "incremental":
        manifest = target.get_json(manifest["parent_manifest"])
        chain.append(manifest)
    chain.reverse()
    chain_ts = [m["timestamp"] for m in chain]
    log_event("restore_chain_resolved", name=name, chain=chain_ts)

    # replay: full first, then each incremental on top
    os.makedirs(target_path, exist_ok=True)
    for m in chain:
        for f in m["files"]:
            key = f"backups/{name}/{m['timestamp']}/{m['type']}/{f['path']}"
            dest = os.path.join(target_path, f["path"])
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            target.get_file(key, dest)
            log_event("file_restored", name=name, file=f["path"],
                      from_backup=m["timestamp"], type=m["type"])
        for path in m["deleted_files"]:
            dest = os.path.join(target_path, path)
            if os.path.exists(dest):
                os.remove(dest)
                log_event("file_removed", name=name, file=path, from_backup=m["timestamp"])

    # permissions, ownership and MD5 verification against the final snapshot
    results = []
    for f in chain[-1]["snapshot"]:
        dest = os.path.join(target_path, f["path"])
        os.chmod(dest, int(f["permissions"], 8))
        try:
            os.chown(dest, f["uid"], f["gid"])
            ownership = "restored"
        except PermissionError:
            ownership = "skipped (needs root)"
        actual_md5 = md5_of_file(dest)
        status = "PASS" if actual_md5 == f["md5"] else "FAIL"
        log_event("checksum_verified", name=name, file=f["path"],
                  expected_md5=f["md5"], actual_md5=actual_md5, status=status)
        results.append({
            "path": f["path"],
            "expected_md5": f["md5"],
            "actual_md5": actual_md5,
            "permissions": f["permissions"],
            "owner": f"{f['owner']}:{f['group']}",
            "ownership": ownership,
            "status": status,
        })

    overall = "PASS" if all(r["status"] == "PASS" for r in results) else "FAIL"
    log_event("restore_completed", name=name, backup_timestamp=timestamp,
              target_path=os.path.abspath(target_path), file_count=len(results), status=overall)

    return {
        "action": "restore",
        "name": name,
        "timestamp": timestamp,
        "type": run["type"],
        "target": run["target"],
        "chain": chain_ts,
        "target_path": os.path.abspath(target_path),
        "file_count": len(results),
        "files": results,
        "overall_status": overall,
    }


def main():
    parser = argparse.ArgumentParser(description="Workload Snapshot & Restore Simulator")
    parser.add_argument("--action", required=True,
                        choices=["define", "backup", "list", "restore"])
    parser.add_argument("--workload-path", help="Workload directory (for define)")
    parser.add_argument("--name", help="Workload name")
    parser.add_argument("--target", choices=["s3", "nfs"], help="Backup target")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true", help="Full backup")
    mode.add_argument("--incremental", action="store_true", help="Incremental backup")
    parser.add_argument("--timestamp", help="Backup timestamp (for restore)")
    parser.add_argument("--target-path", help="Restore destination directory")
    args = parser.parse_args()

    try:
        if args.action == "define":
            if not args.workload_path or not args.name:
                result = {"action": "define", "error": "--workload-path and --name are required"}
            else:
                result = define_action(args.workload_path, args.name)
        elif args.action == "backup":
            if not args.name or not args.target or not (args.full or args.incremental):
                result = {"action": "backup",
                          "error": "--name, --target and one of --full/--incremental are required"}
            else:
                result = backup_action(args.name, args.target,
                                       "full" if args.full else "incremental")
        elif args.action == "list":
            if not args.name:
                result = {"action": "list", "error": "--name is required"}
            else:
                result = list_action(args.name)
        else:
            if not args.name or not args.timestamp or not args.target_path:
                result = {"action": "restore",
                          "error": "--name, --timestamp and --target-path are required"}
            else:
                result = restore_action(args.name, args.timestamp, args.target_path)
    except Exception as e:
        log_event("error", action=args.action, error=str(e))
        result = {"action": args.action, "error": str(e)}

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
