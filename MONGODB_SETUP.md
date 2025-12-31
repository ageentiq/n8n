# MongoDB Integration Guide

## Overview

The `track_message_status.py` script now supports saving WhatsApp message statuses to MongoDB for persistent storage and tracking over time.

## MongoDB Schema

### Collection: `message_statuses` (configurable)

Each document represents a WhatsApp message and its status history:

```json
{
  "_id": ObjectId("..."),
  
  // Message identifiers
  "messageId": "wamid.HBgMOTY2NTQwNDAxNDQ1...",  // Unique WhatsApp message ID
  "conversation_id": "966540401445",             // WhatsApp user ID (recipient)
  
  // Latest status (for quick queries)
  "latestStatus": "read",
  "latestTimestamp": 1767106892,
  "latestTimestampFormatted": "2025-12-30 18:01",  // Asia/Riyadh timezone (UTC+3)
  
  // Full status history
  "statusHistory": [
    {
      "status": "sent",
      "timestamp": 1767106880,
      "timestampFormatted": "2025-12-30 18:01",  // Asia/Riyadh
      "executionId": "25202",
      "conversation_id": "966540401445"
    },
    {
      "status": "delivered",
      "timestamp": 1767106885,
      "timestampFormatted": "2025-12-30 18:01",  // Asia/Riyadh
      "executionId": "25203",
      "conversation_id": "966540401445"
    },
    {
      "status": "read",
      "timestamp": 1767106892,
      "timestampFormatted": "2025-12-30 18:01",  // Asia/Riyadh
      "executionId": "25204",
      "conversation_id": "966540401445"
    }
  ],
  
  // Metadata (all in Asia/Riyadh timezone)
  "workflowId": "tc3qpQJxlWVVTjeq",
  "statusCount": 3,
  "firstSeenAt": ISODate("2025-12-30T15:00:00+03:00"),    // When first discovered
  "lastUpdatedAt": ISODate("2025-12-30T18:01:32+03:00"),  // When status last changed
  "lastScannedAt": ISODate("2025-12-30T19:00:00+03:00")   // Last scan time
}
```

### Automatic Deduplication

The script automatically removes duplicate status entries before saving to MongoDB. Sometimes n8n webhooks fire multiple times for the same status update, causing duplicates. The deduplication logic ensures:

- ✅ Each unique status update is stored only once
- ✅ Duplicates are identified by: `status` + `timestamp` + `executionId`
- ✅ `statusCount` reflects the actual number of unique status changes
- ✅ `statusHistory` array contains no duplicates

**Example**: If n8n sends two identical "delivered" statuses at the same timestamp from the same execution, only one will be saved to MongoDB.

## Timezone Configuration

All timestamps in the application use **Asia/Riyadh timezone (UTC+3)**:

- ✅ **Formatted timestamps**: `timestampFormatted` fields show local Riyadh time
- ✅ **MongoDB metadata**: `firstSeenAt`, `lastUpdatedAt`, `lastScannedAt` in Riyadh time
- ✅ **Unix timestamps**: Remain timezone-agnostic (universal)

**Example**:
```json
{
  "latestTimestamp": 1767106892,              // Unix timestamp (universal)
  "latestTimestampFormatted": "2025-12-30 18:01",  // Asia/Riyadh (UTC+3)
  "lastUpdatedAt": ISODate("2025-12-30T18:01:32+03:00")  // MongoDB datetime with timezone
}
```

**Why Asia/Riyadh?** This matches your local timezone in Saudi Arabia, making it easier to:
- Read logs and understand when events occurred
- Schedule automated jobs
- Debug issues based on local business hours

## Environment Variables

Add these to your `.env` file:

```bash
# MongoDB Configuration
# MONGODB_URI=mongodb://localhost:27017
# Or for MongoDB Atlas:
MONGODB_STATUS_COLLECTION=conversation_status
MONGODB_CONVERSATIONS_COLLECTION=converation_history
MONGODB_DATABASE=Osus_live2
MONGODB_URI="mongodb+srv://ageentiq:Co2x5Lgn0ifjnhyy@cluster0.uofyoid.mongodb.net/Osus_live2?retryWrites=true&w=majority&maxPoolSize=5&maxIdleTimeMS=60000"

```

## Installation

Install the MongoDB Python driver:

```bash
pip install pymongo
```

## Usage

### List Mode with MongoDB

Scan recent messages and save them to MongoDB:

```bash
# Save 50 most recent messages
python3 track_message_status.py --max-messages 50 --save-to-mongodb

# Filter by user and save
python3 track_message_status.py 966532127070 --max-messages 20 --save-to-mongodb

# Filter by time and save
python3 track_message_status.py --since 1767000000 --max-messages 100 --save-to-mongodb
```

### Track Mode with MongoDB

Track a specific message and save to MongoDB:

```bash
python3 track_message_status.py 966532127070 wamid.ABC123 --save-to-mongodb
```

## Indexes

The script automatically creates these indexes on first run for optimal performance:

```javascript
// Created automatically by the script
db.message_statuses.createIndex({ "messageId": 1 }, { unique: true })
db.message_statuses.createIndex({ "conversation_id": 1 })
db.message_statuses.createIndex({ "conversation_id": 1, "latestTimestamp": -1 })
db.message_statuses.createIndex({ "latestStatus": 1 })
```

## Update Behavior

The script uses **upsert** logic:

1.  **New Message**: Inserts a new document with all fields
2.  **Existing Message - Status Changed**: Updates `latestStatus`, `latestTimestamp`, `statusHistory`, and `lastUpdatedAt`
3.  **Existing Message - No Change**: Only updates `lastScannedAt`

This ensures:
- ✅ No duplicate messages (unique constraint on `messageId`)
- ✅ Status history is preserved
- ✅ Latest status is always current
- ✅ Timestamps track when messages were first seen, last updated, and last scanned

## Query Examples

### Find all messages for a user

```javascript
db.message_statuses.find({ "conversation_id": "966532127070" })
  .sort({ "latestTimestamp": -1 })
```

### Find unread messages

```javascript
db.message_statuses.find({ 
  "latestStatus": { $in: ["sent", "delivered"] }
})
```

### Find messages by status

```javascript
db.message_statuses.find({ "latestStatus": "read" })
```

### Find messages updated in the last hour

```javascript
db.message_statuses.find({
  "lastUpdatedAt": {
    $gte: new Date(Date.now() - 3600000)
  }
})
```

### Count messages by status per user

```javascript
db.message_statuses.aggregate([
  {
    $group: {
      _id: { conversation_id: "$conversation_id", status: "$latestStatus" },
      count: { $sum: 1 }
    }
  },
  { $sort: { "_id.conversation_id": 1, "_id.status": 1 } }
])
```

## Scheduled Scans

You can set up automated scans using cron (Linux/Mac) or Task Scheduler (Windows):

### Linux/Mac (crontab)

```bash
# Run every 5 minutes
*/5 * * * * cd /path/to/n8n && python track_message_status.py --max-messages 100 --save-to-mongodb >> /var/log/wa_status.log 2>&1
```

### Windows Task Scheduler

Create a task that runs:
```
python C:\Users\alwlh\OneDrive\Desktop\code\n8n\track_message_status.py --max-messages 100 --save-to-mongodb
```

## Benefits

1. **Historical Tracking**: See how message statuses evolved over time
2. **Analytics**: Query patterns, delivery rates, read rates
3. **Monitoring**: Alert on failed messages
4. **Audit Trail**: Complete history of all status changes
5. **Scalability**: MongoDB handles millions of messages efficiently

## Troubleshooting

### "pymongo not installed"
```bash
pip install pymongo
```

### "MongoDB connection failed"
- Check `MONGODB_URI` is correct
- Ensure MongoDB is running
- Check network/firewall settings
- For Atlas: verify IP whitelist

### No data being saved
- Ensure `--save-to-mongodb` flag is used
- Check `.env` has correct MongoDB variables
- Look for `[mongodb]` messages in stderr output
