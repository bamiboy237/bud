"""Core of bud: an append-only event log, the hub that wakes readers, and the runner that
turns agent output into events. The HTTP server in `server.py` and the terminal client in
`client.py` sit on top of these.
"""
