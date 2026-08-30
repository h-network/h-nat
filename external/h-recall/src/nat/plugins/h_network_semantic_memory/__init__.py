"""h-network-semantic-memory — NAT plugin: long-term semantic memory.

Workflow-callable surface:

  - ``_type: h_semantic_search``    — hybrid retrieval (text + vector via RRF)
  - ``_type: h_semantic_sweep``     — one bounded hot→audit migration pass
  - ``_type: h_semantic_vectorize`` — one bounded embed-pending pass

See :mod:`.register` for the spec and the implementation entry points.
Library-level building blocks (the async :class:`AuditStore`, the
:class:`FastEmbedEmbedder` wrapper, the migrate / vectorize helpers,
and the RediSearch query-escape) are under :mod:`._internal`.

Per ADR-012, this module's scope tag is ``chat-audit``:
``<pod>:<agent>:chat-audit:<chat_id>:<ts_ns>`` for data,
``<pod>:<agent>:chat-audit:idx`` for the tenant-wide RediSearch index.
"""

from ._internal.store import AuditStore  # re-export for harness use

__all__ = ["AuditStore"]
