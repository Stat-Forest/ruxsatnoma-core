"""Transactional outbox for domain events.

An event is written in the same transaction as the change to the
aggregate that produced it, and delivered afterwards by the integration
service. Available to every module and importing none of them.
"""
