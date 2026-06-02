"""Gedeelde data structuur (de "temptable").
Alle fases lezen/schrijven naar /tmp/domus-temptable.json

Structuur:
{
  "collection": {
    "immoweb": {"status": "pending|running|done|failed", "ids": ["id1",...]},
    "zimmo": {"status": "pending|running|done|failed", "ids": [...]},
    "immovlan": {"status": "pending|running|done|failed", "ids": [...]},
    "tweedehands": {"status": "pending|running|done|failed", "ids": [...]},
    "immoscoop": {"status": "pending|running|done|failed", "ids": [...]}
  },
  "detail_batches": {
    "immoweb_batch_0": {"status": "pending|running|done|failed", "listings": [...]},
    "immoweb_batch_1": {"status": "...", "listings": [...]},
    ...
  },
  "processed": {
    "status": "pending|running|done|failed",
    "listings": [...],
    "total": 0,
    "after_dedup": 0
  },
  "scored": {
    "status": "pending|running|done|failed",
    "listings": [...],
    "deepseek_done": false
  },
  "email": {
    "status": "pending|running|done|failed",
    "sent_at": null
  }
}
"""

import json
import os

TEMP_TABLE = "/tmp/domus-temptable.json"

def read():
    if not os.path.exists(TEMP_TABLE):
        return _empty()
    with open(TEMP_TABLE) as f:
        return json.load(f)

def write(data):
    with open(TEMP_TABLE, "w") as f:
        json.dump(data, f, indent=2)

def _empty():
    return {
        "collection": {},
        "detail_batches": {},
        "processed": {"status": "pending", "listings": [], "total": 0, "after_dedup": 0},
        "scored": {"status": "pending", "listings": [], "deepseek_done": False, "gemini_done": True},
        "email": {"status": "pending", "sent_at": None}
    }

def init():
    """Reset de temptable voor een nieuwe run"""
    write(_empty())
    return True

def update_collection(scraper_name, status, ids=None):
    data = read()
    data["collection"][scraper_name] = {
        "status": status,
        "ids": ids or []
    }
    write(data)

def add_batch(batch_key, listings):
    data = read()
    data["detail_batches"][batch_key] = {
        "status": "done",
        "listings": listings
    }
    write(data)

def set_processed(listings):
    data = read()
    data["processed"] = {
        "status": "done",
        "listings": listings,
        "total": len(listings),
        "after_dedup": len(listings)
    }
    write(data)
