#!/usr/bin/env python3
"""Collecte immoweb search results -> temptable"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scrapers.immoweb import ImmowebScraper
from phases._schema import update_collection

try:
    s = ImmowebScraper()
    results = s.search()
    ids = [r.get("id") or r.get("url", "") for r in results]
    update_collection("immoweb", "done" if ids else "failed", ids)
    print(f"immoweb: {len(ids)} listings")
except Exception as e:
    print(f"immoweb FAILED: {e}")
    update_collection("immoweb", "failed", [])
