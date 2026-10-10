"""memk - a memory kernel for agents.

The package is deliberately small and dependency-free: SQLite and the standard
library, nothing else. Optional capabilities (an embedding channel, a host
adapter) plug in through explicit interfaces so that a missing optional piece can
never silently become the whole retrieval story.

Typical use::

    from memk import open_store, Scope
    from memk import journal, admission, recall, packet

    conn = open_store("memory.sqlite3")
    scope = Scope(platform="cli", workspace="default", agent="assistant", user="alice")
    captured = journal.capture_turn(conn, scope=scope, session_id="s1", turn=1,
                                    role="user", content="I deploy on Fridays.")
    candidate = admission.propose_from_turn(role="user", content="...",
                                            journal_ids=captured.journal_ids)
    decision = admission.admit(conn, scope=scope, candidate=candidate)
    hits = recall.search(conn, scope, "when do I deploy")
    print(packet.build("when do I deploy", hits).render())
"""

from .scope import Scope
from .schema import connect, initialise, open_store, schema_version
from .session import Memory, open_memory

__all__ = [
    "Memory",
    "Scope",
    "connect",
    "initialise",
    "open_memory",
    "open_store",
    "schema_version",
    "__version__",
]
__version__ = "0.1.0"
