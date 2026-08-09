"""Bundled local embedding models (offline distribution support).

fetch.py pre-pulls the whitelisted fastembed models into this directory
using the standard Hugging Face hub cache layout — exactly what fastembed
produces at runtime — so the app loads them fully offline. Model weights
are gitignored; only this script and the README are tracked.
"""
