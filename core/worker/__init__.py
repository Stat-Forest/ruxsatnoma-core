"""Scheduled jobs for the core service.

Runs as a separate process (core-worker) from the same image as the API, with a
different entry point. Owns the recurring work the specification requires:

* expiring permits once their validity period ends
* releasing contour occupancy behind expired or cancelled permits
* creating the next monthly partition of the audit log
* relaying outbox messages that the inline relay failed to deliver
* reconciling payments against the bank statement
* moving documents into the archive once their retention period starts

Scheduling is done with APScheduler. Jobs call the public API of the modules
they need; they never reach into another module's tables.

Owner: technical lead. See architecture/modules.md.
"""
