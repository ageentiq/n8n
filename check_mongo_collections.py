
import os
from pymongo import MongoClient
import sys

# Load env manually since we are in a simple script
env_path = ".env"
if os.path.exists(env_path):
    with open(env_path, "r") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ[k] = v.strip('"\'')

uri = os.getenv("MONGODB_URI")
db_name = os.getenv("MONGODB_DATABASE")

if not uri or not db_name:
    print("Missing env vars")
    sys.exit(1)

try:
    client = MongoClient(uri)
    db = client[db_name]
    print(f"Connected to {db_name}")
    print("Collections:")
    for name in db.list_collection_names():
        print(f" - {name}")
except Exception as e:
    print(f"Error: {e}")
