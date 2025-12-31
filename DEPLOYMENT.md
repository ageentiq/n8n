# Deployment Guide: n8n WhatsApp Status Tracker

This guide explains how to deploy the `track_message_status.py` script on a Linux cloud server (Ubuntu/Debian recommended) and run it every 20 seconds.

## 1. System Requirements

- **OS**: Ubuntu 20.04/22.04 LTS or Debian 10/11
- **Python**: 3.8 or higher
- **RAM**: Minimal (512MB is sufficient)

## 2. Dependencies Installation

Run the following commands on your server:

```bash
# Update package list
sudo apt update

# Install Python 3 and pip (if not installed)
sudo apt install -y python3 python3-pip python3-venv

# Create a project directory
mkdir -p ~/n8n-tracker
cd ~/n8n-tracker
```

## 3. Script Setup

Copy your files to the server:
- `track_message_status.py`
- `.env`

Or create them directly on the server.

### Install Python Libraries

It is recommended to use a virtual environment:

```bash
# Create virtual environment
python3 -m venv venv

# Activate it
source venv/bin/activate

# Install required libraries
pip install requests pymongo
```

## 4. Environment Configuration

Ensure your `.env` file is in `~/n8n-tracker/.env` and has the correct values:

```bash
WORKFLOW_ID_WHATSAPP=tc3qpQJxlWVVTjeq
N8N_BASE_URL=https://n8n.agentiq.llc
N8N_API_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIwOWU1MmVhMC1jOTBjLTQyMjYtOGY5Ny1jNzM5ZWU3ZWJhNDciLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwiaWF0IjoxNzY2Mzg1NjU5fQ.UMwRweOXEvEzuEyZMKjzDEOsyU-piPX-7p_v5eCAhWM
MONGODB_URI="mongodb+srv://ageentiq:Co2x5Lgn0ifjnhyy@cluster0.uofyoid.mongodb.net/Osus_live2?retryWrites=true&w=majority&maxPoolSize=5&maxIdleTimeMS=60000"
MONGODB_DATABASE=Osus_live2
MONGODB_CONVERSATIONS_COLLECTION=converation_history
MONGODB_STATUS_COLLECTION=conversation_status
```

## 5. Scheduling (Every 20 Seconds)

Since standard `cron` only runs every minute, we need a trick to run every 20 seconds. We will add 3 cron entries with `sleep` delays.

### Step 1: Open Crontab

```bash
crontab -e
```

### Step 2: Add the Schedule

Add these 3 lines to the bottom of the file:

```bash
# Run immediately at the start of the minute
* * * * * cd /home/ageentiq/ms_status && /home/ageentiq/ms_status/venv/bin/python3 track_message_status.py --max-messages 100 --save-to-mongodb >> /home/ageentiq/ms_status/tracker.log 2>&1

# Run with 20 second delay
* * * * * sleep 20 && cd /home/ageentiq/ms_status && /home/ageentiq/ms_status/venv/bin/python3 track_message_status.py --max-messages 100 --save-to-mongodb >> /home/ageentiq/ms_status/tracker.log 2>&1

# Run with 40 second delay
* * * * * sleep 40 && cd /home/ageentiq/ms_status && /home/ageentiq/ms_status/venv/bin/python3 track_message_status.py --max-messages 100 --save-to-mongodb >> /home/ageentiq/ms_status/tracker.log 2>&1
```

### Step 3: Verify

Save and exit. Check the logs after a minute:

```bash
tail -f ~/ms_status/tracker.log
```

## 6. (Optional) Log Rotation

To prevent the log file from growing too large, setup `logrotate`.

Create file `/etc/logrotate.d/n8n-tracker`:

```bash
/home/ageentiq/ms_status/tracker.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
}
```

## Quick Checklist
- [ ] Python 3.8+ installed
- [ ] Requirements (`requests`, `pymongo`) installed
- [ ] `.env` file configured with MongoDB URI
- [ ] Script tested manually once (`./venv/bin/python3 track_message_status.py ...`)
- [ ] Crontab configured with 3 entries
