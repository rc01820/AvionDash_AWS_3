"""Build the Lambda code package for cloudformation/avionpulse.yaml.

    python scripts/package.py                         # -> dist/avionpulse-code-<hash>.zip
    python scripts/package.py --bucket my-artifacts   # ...and upload it with the AWS CLI

Zip layout: the Python modules from src/ at the root (Lambda handlers such as
api.handler), the SPA under web/ (published by the site deployer), and
classic_tier/ — the Classic stack template the api function launches plus the
gate service the Classic EC2 instances download. The file
name carries a content hash, so a new upload = a new CodeKey = CloudFormation
updates every function and redeploys the site. Standard library only.
"""
import argparse
import hashlib
import io
import os
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXED_TIME = (2026, 1, 1, 0, 0, 0)  # identical inputs -> identical zip -> identical hash


def _entries():
    src = os.path.join(ROOT, "src")
    for name in sorted(os.listdir(src)):
        if name.endswith(".py"):
            yield os.path.join(src, name), name
    yield os.path.join(src, "classic_tier", "gate_service.py"), "classic_tier/gate_service.py"
    yield os.path.join(ROOT, "cloudformation", "classic.yaml"), "classic_tier/classic.yaml"
    web = os.path.join(ROOT, "web")
    for dirpath, dirnames, names in os.walk(web):
        dirnames.sort()
        for name in sorted(names):
            full = os.path.join(dirpath, name)
            yield full, "web/" + os.path.relpath(full, web).replace(os.sep, "/")


def build():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for full, arc in _entries():
            info = zipfile.ZipInfo(arc, FIXED_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(full, "rb") as fh:
                zf.writestr(info, fh.read())
    data = buf.getvalue()
    digest = hashlib.sha256(data).hexdigest()[:10]
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    path = os.path.join(ROOT, "dist", f"avionpulse-code-{digest}.zip")
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--bucket", help="upload to this S3 bucket (needs the AWS CLI)")
    p.add_argument("--prefix", default="avionpulse/", help="key prefix inside the bucket")
    p.add_argument("--region", default=os.environ.get("AWS_REGION"))
    a = p.parse_args()

    path = build()
    size_kb = os.path.getsize(path) / 1024
    key = a.prefix + os.path.basename(path)
    print(f"built {os.path.relpath(path, ROOT)} ({size_kb:.0f} KB)")
    if a.bucket:
        cmd = ["aws", "s3", "cp", path, f"s3://{a.bucket}/{key}"]
        if a.region:
            cmd += ["--region", a.region]
        if subprocess.call(cmd, shell=(os.name == "nt")) != 0:
            sys.exit("upload failed")
        print(f"uploaded s3://{a.bucket}/{key}")
    print(f"CodeKey={key}")


if __name__ == "__main__":
    main()
