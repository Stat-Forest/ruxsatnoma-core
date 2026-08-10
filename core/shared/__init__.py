"""Facilities available to every module.

Audit, outbox and classifiers may be imported by anyone. They import no domain
module themselves: a cycle here would make extracting anything impossible.
"""
