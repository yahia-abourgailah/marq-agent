"""
[claude] Who a request is being made on behalf of.

This is the object docs/HANDOFF.md has been deferring to "the API layer, when
it exists". Two identifiers in the graph — `requester_id` and `workspace_id` —
are described there as things that must come from an authenticated session and
never from user input, and this is where that becomes true rather than
aspirational.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# [claude] Prefix for a derived workspace id. Present so a workspace
# directory is recognisable on disk as one this layer minted, rather than
# something a test or a script created by hand.
WORKSPACE_PREFIX = "ws"


def workspace_id_for(subject: str) -> str:
    """
    The workspace id belonging to one authenticated subject.

    [claude] Derived, never chosen. A caller who could name their own
    workspace could name someone else's, which is design decision 9 in
    docs/HANDOFF.md — the same reasoning that keeps it out of the tool
    schemas applies with more force at the edge, where the caller is a
    browser rather than a model.

    Hashed rather than used directly, for two reasons:

    *   `WorkspaceStore` validates ids against
        `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` and rejects anything else. A
        subject claim is commonly an email or a namespaced id, and `@`, `.`
        and `|` are all outside that pattern — so passing it through would
        fail for realistic tokens and, worse, fail only for *some* users.

    *   It keeps the identity off the filesystem. The workspace id becomes a
        directory name under `settings.workspace_root`; an email address as
        a directory name puts personal data into backups, logs and stack
        traces that have no reason to carry it.

    Deterministic, so the same employee returns to the same workspace across
    restarts and across processes.
    """

    digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()

    return f"{WORKSPACE_PREFIX}{digest[:32]}"


@dataclass(frozen=True)
class Principal:
    """
    An authenticated caller.

    Frozen because a route handler holding one must not be able to edit the
    identity it was handed and pass it further down.
    """

    subject: str
    scopes: tuple[str, ...] = ()

    @property
    def workspace_id(self) -> str:
        """This caller's workspace. Derived from the subject; see above."""

        return workspace_id_for(self.subject)

    @property
    def requester_id(self) -> str:
        """
        The employee id published to PostgreSQL as `app.requester_id`.

        The same value as `subject`, named separately because that is what
        the graph, `SQLExecutor` and the RLS policy in
        `migrations/002_row_level_security.sql` call it. Keeping the name at
        the boundary makes the path greppable end to end.
        """

        return self.subject

    def thread_key(self, thread_id: str) -> str:
        """
        Namespace a client-supplied thread id to this caller.

        [claude] The security property behind the whole thread API. A thread
        id arrives from the browser, so it is user input and cannot be
        trusted as a key into shared conversation storage — without this,
        asking for someone else's thread id would return their conversation,
        which is a read of arbitrary CRM answers belonging to another
        employee.

        Namespacing rather than checking-then-using is deliberate: there is
        no path that forgets the check, because there is no unnamespaced
        key. Two users asking for thread "today" each get their own.
        """

        return f"{workspace_id_for(self.subject)}:{thread_id}"


__all__ = ["Principal", "WORKSPACE_PREFIX", "workspace_id_for"]
