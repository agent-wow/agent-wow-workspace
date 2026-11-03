"""Autonomous growth runtime for the five-character MVP.

The package is deliberately standard-library only. It owns:

- the per-character checkpoint SQLite store (single writer, WAL),
- roster/policy/identity gates,
- the OS worker lock and operator STOP handling,
- the non-destructive event consumer and epoch reconciliation,
- the independent coordinator/validator store boundary,
- backup/restore and permission audits.

Gameplay strategy belongs to later units. Nothing in this package starts or
stops a game session: session-keeper.py is the sole session-action owner.
"""

__version__ = "0.1.0"
