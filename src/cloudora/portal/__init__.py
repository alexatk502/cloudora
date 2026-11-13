"""Cloudora's web portal: the sign-in page, the screen viewer and the WebSocket that carries the screen.

Security model: docs/SECURITY.md. Pages run only Cloudora's own scripts (the hash-pinned sign-in script and
/static/viewer.js); the WebSocket checks Origin, the sign-in cookie and a session-bound token before anything else.
"""
