"""Append-only audit journal.

Every legally significant action is written here and can never be
changed or deleted, not even by an administrator. Available to every
module and importing none of them.
"""
