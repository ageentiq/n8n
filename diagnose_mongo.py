
import os
import sys
from pymongo import MongoClient
import json

# Load env
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

print(f"Connecting to DB: {db_name}, Col: {col_name}")

if not uri:
    print("No URI")
    sys.exit(1)

client = MongoClient(uri)
db = client[db_name]
collection = db[col_name]

# 1. Count documents
count = collection.count_documents({})
print(f"Total documents: {count}")

# 2. Check for the specific phone number
wa_id = "966530279161"
query = {"conversation_id": wa_id}
user_docs = list(collection.find(query).limit(5))
print(f"Documents for {wa_id}: {len(user_docs)}")

if user_docs:
    print("Sample doc keys:", user_docs[0].keys())
    print("Sample message_id:", user_docs[0].get("message_id"))
else:
    print("No documents found for this user.")

# 3. Check for any message_id to ensure field name is correct
sample = collection.find_one()
if sample:
    print("Random doc message_id:", sample.get("message_id"))
    print("Random doc conversation_id:", sample.get("conversation_id"))
