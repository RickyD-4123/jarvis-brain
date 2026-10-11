"""The agent's own memory: the same file-based memory Claude Code keeps between sessions.

Jarvis's work mode and his self-builder are Claude Code sessions. Like a Claude Code session, they keep
notes for their future selves in a folder (data/agent-memory/): one fact per file, and MEMORY.md as the
index, which is handed to every new session. They save what the user teaches them about how to work,
decisions and why, project history, gotchas, and where things are. memory.py's facts about the user
complement this; the conversation log (conversation.py) holds what was actually said.
"""
import os

PROTOCOL = """# Your memory

You have a persistent, file-based memory at `{path}`. It is how you remember across sessions, the way a
person keeps notes: write to it directly with your file tools (the folder exists). Each memory is one file
holding one fact, with frontmatter:

```markdown
---
name: <short-kebab-case-slug>
description: <one-line summary, used to decide relevance later>
type: user | feedback | project | reference
---

<the fact; for feedback/project, follow with **Why:** and **How to apply:** lines. Link related memories with [[their-name]].>
```

`user`: who the user is (role, studies, preferences). `feedback`: guidance the user gave on how you should
work, corrections and confirmed approaches, with the why. `project`: ongoing work, goals, decisions and
constraints not obvious from the code or git history; convert relative dates to absolute. `reference`:
pointers to resources (folders, URLs, documents).

After writing a file, add a one-line pointer to `MEMORY.md` in that folder (`- [Title](file.md): hook`).
MEMORY.md is the index you are given at the start of every session: one line per memory, never the
memory's content. Before saving, check for an existing file that covers it and update it instead of
duplicating; delete memories that turn out to be wrong. Don't save what the code or git history already
records, or what only matters to this conversation. Save when you learn something durable: the user
corrects you, states a preference, makes a decision, or you hit a non-obvious problem and its fix. Do it
as you go, not only at the end.

The full conversation log is `{log}` (JSON lines: t, kind, user, jarvis): search it (grep) when the user
refers to something said before that isn't in front of you. What memory.py has learned about the user is
given to you separately.

## MEMORY.md (current index)

{index}"""


def folder(home):
    return os.path.join(home, "data", "agent-memory")


def ensure(home):
    path = folder(home)
    os.makedirs(path, exist_ok=True)
    index = os.path.join(path, "MEMORY.md")
    if not os.path.isfile(index):
        with open(index, "w", encoding="utf-8") as f:
            f.write("")
    return path


def protocol(home):
    """The memory instructions plus the current index, for a session's system prompt."""
    path = ensure(home)
    try:
        with open(os.path.join(path, "MEMORY.md"), encoding="utf-8") as f:
            index = f.read().strip()
    except OSError:
        index = ""
    return PROTOCOL.format(path=path.replace("\\", "/"), log=os.path.join(home, "data", "history",
                                                                          "conversations.jsonl").replace("\\", "/"),
                           index=index or "(empty: nothing saved yet)")
