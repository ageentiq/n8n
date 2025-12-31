
import os
import sys
from pymongo import MongoClient

# Load env variables manually
env_path = ".env"
if os.path.exists(env_path):
    with open(env_path, "r") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ[k] = v.strip('"\'')

uri = os.getenv("MONGODB_URI")
db_name = os.getenv("MONGODB_DATABASE")
col_name = os.getenv("MONGODB_CONVERSATIONS_COLLECTION")

if not uri:
    print("Error: detailed env not loaded")
    sys.exit(1)

client = MongoClient(uri)
db = client[db_name]
collection = db[col_name]

wa_id = "966530279161"
print(f"Checking DB: {db_name}.{col_name} for conversation_id: {wa_id}")

# Find documents for this user
docs = list(collection.find({"conversation_id": wa_id}, {"message_id": 1, "timestamp": 1, "_id": 0}))

print(f"Found {len(docs)} documents.")
for i, doc in enumerate(docs):
    print(f"Doc {i+1}: {doc}")
