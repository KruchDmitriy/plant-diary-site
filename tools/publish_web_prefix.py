#!/usr/bin/env python3
import os, json, sys
from pathlib import Path
try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    print("Install: pip install boto3 python-dotenv", file=sys.stderr); raise
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

bucket=os.getenv("YC_BUCKET","plant-diary-photos")
endpoint=os.getenv("YC_S3_ENDPOINT","https://storage.yandexcloud.net")
key=os.getenv("YC_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID")
secret=os.getenv("YC_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY")
if not key or not secret:
    raise SystemExit("No S3 credentials found. Use the same .env variables as the photo uploader.")

s3=boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=key, aws_secret_access_key=secret, region_name="ru-central1")
try:
    raw=s3.get_bucket_policy(Bucket=bucket).get("Policy","")
    policy=json.loads(raw) if raw else {"Version":"2012-10-17","Statement":[]}
except ClientError as e:
    code=e.response.get("Error",{}).get("Code","")
    if code in {"NoSuchBucketPolicy","NoSuchPolicy","404"}:
        policy={"Version":"2012-10-17","Statement":[]}
    else:
        raise
st=policy.get("Statement",[])
if isinstance(st,dict): st=[st]
st=[x for x in st if x.get("Sid")!="PlantDiaryPublicWeb"]
st.append({
    "Sid":"PlantDiaryPublicWeb",
    "Effect":"Allow",
    "Principal":"*",
    "Action":"s3:GetObject",
    "Resource":f"arn:aws:s3:::{bucket}/web/*",
    "Condition":{"Bool":{"aws:SecureTransport":"true"}}
})
policy["Statement"]=st
s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy,ensure_ascii=False))
print(f"OK: anonymous HTTPS GetObject enabled only by this policy for s3://{bucket}/web/*")
print(f"Originals are not exposed by this statement: s3://{bucket}/originals/*")
