"""What the user has selected in the app, as context for the chat (no Qt).

"Ask Jarvis" in the inspector starts a normal chat conversation about the
selected graph node. The chat gets a SELECTED block (``chat.build_context``
``focus``) built here from the synced data only: the item itself (an issue's
number, title, state, milestone, labels and description; a commit's message;
a milestone's progress; a check's verified result) and its relationships from
the graph, each with how certain it is. "Commit names the issue" is a
reference in the commit message; "may have closed it" is a text similarity
Jarvis computed, never proof. File names are only those mentioned in the
issue or commit text: Jarvis has no copy of the source code, and the block
says so, so the model can say what is missing instead of guessing.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

BODY_CHARS = 900
MAX_RELATED = 12
CERTAINTY = {
    "mentions": "the commit message names it (a reference, not proof of a fix)",
    "maybe": "UNCONFIRMED: Jarvis found the commit text similar; not proof it fixed anything",
    "milestone": "assigned on GitHub",
    "card": "a suggestion Jarvis made from the checks",
    "check": "named by an electrical check",
    "history": "the previous commit",
    "hub": "grouping only",
    "project": "assigned in Jarvis",
    "about": "linked in Jarvis",
    "related": "linked in Jarvis",
    "judged": "a research paper judged against it (relevance is the model's opinion)",
}
NO_SOURCE = ("Source code: Jarvis has no copy of the repository's code; only issue and commit "
             "text, milestones and hardware.yaml are synced. File names below are only those "
             "mentioned in that text.")


@dataclass(frozen=True)
class Focus:
    node_id: str
    kind: str          # the node's kind ("issue", "commit", ...)
    label: str         # shown on the chat's context chip: "#12 5V rail browns out"
    action: str        # the inspector's button: "Ask Jarvis about this issue"
    question: str      # the first question it asks
    lines: tuple[str, ...]   # the SELECTED block


def _row(conn: sqlite3.Connection, sql: str, args: tuple):
    try:
        return conn.execute(sql, args).fetchone()
    except sqlite3.Error:
        return None


def _labels(raw) -> str:
    import json

    try:
        return ", ".join(str(x) for x in json.loads(raw or "[]")) or "none"
    except ValueError:
        return "none"


def _files(*texts: str) -> list[str]:
    from jarvis.facts import file_names

    out: set[str] = set()
    for t in texts:
        out |= file_names(t or "")
    return sorted(out)


def _related(graph, node_id: str) -> list[str]:
    if graph is None:
        return []
    out = []
    for edge, other in graph.neighbours(node_id)[:MAX_RELATED]:
        if other.kind in ("hub", "core"):
            continue
        name = other.title if other.kind not in ("issue",) else f"{other.label} {other.title}"
        from jarvis.gui.brain_data import EDGE_LABELS

        rel = EDGE_LABELS.get(edge.kind, edge.kind)
        status = f", {other.status}" if other.status else ""
        out.append(f"- {rel}: {other.kind} {name}{status} [{CERTAINTY.get(edge.kind, 'linked')}]")
    return out


def build_focus(conn: sqlite3.Connection, repo: str, node, graph=None) -> Focus | None:
    """The chat context for a graph node; None for nodes with nothing to ask about."""
    if node is None:
        return None
    kind = node.kind
    head = [f"Repository: {repo}"]
    related = _related(graph, node.id)
    rel_block = (["Relationships (from the project graph):", *related] if related
                 else ["Relationships: none in the graph."])
    if kind == "issue":
        num = int(node.id.split(":", 1)[1])
        r = _row(conn, "SELECT i.*, m.title AS ms_title, m.due_on AS ms_due FROM issues i "
                       "LEFT JOIN milestones m ON m.number = i.milestone_number "
                       "WHERE i.number = ?", (num,))
        if r is None:
            return None
        body = (r["body"] or "").strip()
        lines = [*head, f"Issue #{num}: {r['title']}", f"State: {r['state']}",
                 f"Milestone: {r['ms_title'] or 'none'}"
                 + (f" (due {r['ms_due'][:10]})" if r["ms_due"] else ""),
                 f"Labels: {_labels(r['labels'])}",
                 "Description: " + (body[:BODY_CHARS] + ("…" if len(body) > BODY_CHARS else "")
                                    if body else "(empty)"),
                 *rel_block]
        commit_text = " ".join(o.title for _, o in (graph.neighbours(node.id) if graph else [])
                               if o.kind == "commit")
        files = _files(body, r["title"], commit_text)
        lines += [NO_SOURCE, "Files mentioned: " + (", ".join(files) if files else "none")]
        label = f"#{num} {r['title']}"
        return Focus(node.id, kind, label, "Ask Jarvis about this issue",
                     f"Help me with issue #{num}: what does it mean, what should I investigate "
                     "first, which files are likely involved, and could one of the related "
                     "commits already have fixed it?", tuple(lines))
    if kind == "commit":
        sha = node.label
        r = _row(conn, "SELECT * FROM commits WHERE sha LIKE ?", (f"{sha}%",))
        msg = (r["message"] if r else node.title) or ""
        lines = [*head, f"Commit {sha}" + (f" by {r['author']} on {r['committed_at'][:10]}"
                                           if r and r["committed_at"] else ""),
                 "Message: " + msg.strip()[:BODY_CHARS], *rel_block, NO_SOURCE,
                 "Files mentioned: " + (", ".join(_files(msg)) or "none")]
        return Focus(node.id, kind, f"commit {sha}", "Ask Jarvis about this commit",
                     f"What did commit {sha} change, and which issues could it relate to?",
                     tuple(lines))
    if kind == "milestone":
        num = int(node.id.split(":", 1)[1])
        r = _row(conn, "SELECT * FROM milestones WHERE number = ?", (num,))
        if r is None:
            return None
        lines = [*head, f"Milestone: {r['title']} ({r['state']})",
                 f"Due: {r['due_on'][:10] if r['due_on'] else 'no due date'}",
                 f"Issues: {r['closed_issues'] or 0} closed, {r['open_issues'] or 0} open",
                 *rel_block]
        return Focus(node.id, kind, f"milestone {r['title']}", "Ask Jarvis about this milestone",
                     f"What is left for the {r['title']} milestone, and what should I do next "
                     "to finish it?", tuple(lines))
    if kind in ("check", "card"):
        ref = node.id.split(":", 1)[1]
        lines = [*head, f"{'Electrical check' if kind == 'check' else 'Suggestion'}: {node.title}"
                 f" (id {ref})",
                 f"Status: {node.status or 'unknown'}", f"Details (verified): {node.detail}",
                 *rel_block, "Source: hardware.yaml (the checks' only input)."]
        what = "finding" if kind == "check" else "suggestion"
        return Focus(node.id, kind, node.title, f"Ask Jarvis about this {what}",
                     f"Explain this {what} in plain words: what it means, why it matters, and "
                     "what I should check or change.", tuple(lines))
    if kind == "item":
        lines = [*head, f"{node.sub or 'item'} (the user's own, in Jarvis): {node.title}",
                 f"Status: {node.status or 'open'}", f"Details: {node.detail}", *rel_block]
        return Focus(node.id, kind, node.title, f"Ask Jarvis about this {node.sub or 'item'}",
                     f"Help me with \"{node.title}\": what is involved and what should I do "
                     "first?", tuple(lines))
    if kind in ("part", "paper"):
        lines = [*head, f"{kind}: {node.title}", f"Details: {node.detail}", *rel_block]
        return Focus(node.id, kind, node.title, f"Ask Jarvis about this {kind}",
                     f"What should I know about {node.title} for this project?", tuple(lines))
    return None   # the core and hubs: the chat's normal context already covers them
