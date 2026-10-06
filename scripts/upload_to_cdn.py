#!/usr/bin/env python3
"""
Cloudflare R2 CDN uploader for FunnelAgent.

Usage:
    # Upload single file — prints CDN URL to stdout
    python scripts/upload_to_cdn.py --file output/run/logo.png --key run-name/logo.png

    # Upload all images in a directory — prints JSON {filename: cdn_url} to stdout
    python scripts/upload_to_cdn.py --directory output/run/advertorial_images/ --prefix run-name/advertorial_images/

Environment:
    R2_ACCESS_KEY_ID     — S3-compatible access key
    R2_SECRET_ACCESS_KEY — S3-compatible secret key
    R2_ENDPOINT_URL      — e.g. https://<account-id>.r2.cloudflarestorage.com
    R2_BUCKET_NAME       — bucket name
    R2_PUBLIC_URL        — public base URL (custom domain or *.r2.dev)
"""

import argparse
import json
import mimetypes
import os
import sys
from pathlib import Path

try:
    import boto3
    from botocore.config import Config
except ImportError:
    print("Error: boto3 package not installed. Run: pip install boto3", file=sys.stderr)
    sys.exit(1)

try:
    from dotenv import load_dotenv
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")
except ImportError:
    pass


def get_r2_client():
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    endpoint = os.environ.get("R2_ENDPOINT_URL")

    if not all([access_key, secret_key, endpoint]):
        print("Error: R2 credentials not set. Required: R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ENDPOINT_URL", file=sys.stderr)
        sys.exit(1)

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def get_content_type(file_path):
    content_type, _ = mimetypes.guess_type(str(file_path))
    return content_type or "application/octet-stream"


def upload_file(client, bucket, file_path, key, public_url):
    content_type = get_content_type(file_path)
    print(f"Uploading {file_path} → {key} ({content_type})", file=sys.stderr)

    client.upload_file(
        str(file_path),
        bucket,
        key,
        ExtraArgs={"ContentType": content_type},
    )

    cdn_url = f"{public_url.rstrip('/')}/{key}"
    return cdn_url


def main():
    parser = argparse.ArgumentParser(description="Upload images to Cloudflare R2 CDN")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", help="Path to a single file to upload")
    group.add_argument("--directory", help="Path to a directory of images to upload")
    parser.add_argument("--key", help="R2 object key for single file upload")
    parser.add_argument("--prefix", help="R2 key prefix for directory upload")
    args = parser.parse_args()

    bucket = os.environ.get("R2_BUCKET_NAME")
    public_url = os.environ.get("R2_PUBLIC_URL")

    if not bucket or not public_url:
        print("Error: R2_BUCKET_NAME and R2_PUBLIC_URL must be set.", file=sys.stderr)
        sys.exit(1)

    client = get_r2_client()

    if args.file:
        if not args.key:
            print("Error: --key is required when using --file", file=sys.stderr)
            sys.exit(1)

        file_path = Path(args.file)
        if not file_path.exists():
            print(f"Error: File not found: {file_path}", file=sys.stderr)
            sys.exit(1)

        cdn_url = upload_file(client, bucket, file_path, args.key, public_url)
        print(cdn_url)

    elif args.directory:
        if not args.prefix:
            print("Error: --prefix is required when using --directory", file=sys.stderr)
            sys.exit(1)

        dir_path = Path(args.directory)
        if not dir_path.is_dir():
            print(f"Error: Directory not found: {dir_path}", file=sys.stderr)
            sys.exit(1)

        results = {}
        for file_path in sorted(dir_path.iterdir()):
            if not file_path.is_file():
                continue
            if file_path.name.startswith("_"):
                continue

            key = f"{args.prefix.rstrip('/')}/{file_path.name}"
            cdn_url = upload_file(client, bucket, file_path, key, public_url)
            results[file_path.name] = cdn_url

        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
