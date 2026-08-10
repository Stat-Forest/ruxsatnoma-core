# Ruxsatnoma core — lessons

Hard-won gotchas from real bugs, incidents and review findings. Every rule here
is specific to Ruxsatnoma — general Python advice lives in CLAUDE.md or in your
head, not here.

**When you fix a non-trivial bug or discover a non-obvious pattern, APPEND a new
lesson.** The `/implement` command reads this file before any work starts —
every entry saves a future agent from repeating a mistake.

Format per entry:

```
## {Short topic title}
- **Rule:** {one line}
- **Why:** {one line — past incident, strong preference, or hidden invariant}
- **How to apply:** {one line — when and where this guidance kicks in}
```

The file starts empty on purpose: a lesson earns its place by costing us
something first.

---
