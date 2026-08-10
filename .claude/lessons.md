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

## Repository secrets are absent from dependabot and fork runs

- **Rule:** A workflow step that needs `secrets.*` must tolerate them being empty. GitHub does not expose repository secrets to runs it starts on behalf of dependabot — those read a separate Dependabot secrets store — nor to pull requests from forks. Check the secret and skip with exit 0 rather than letting the step fail.
- **Why:** 2026-08-10, the day `notify-telegram.yml` landed. Every dependabot pull request turned the check red with `curl: (22) 404` — `TG_BOT_TOKEN` expanded to an empty string and the URL became `api.telegram.org/bot/sendMessage`. The secrets were set correctly; the same workflow passed on human events minutes earlier. A permanently red check on bot PRs teaches the team to stop reading checks, which costs more than a missed notification.
- **How to apply:** Any new workflow reading a secret gets the same guard at the top of its script: `if [ -z "${SECRET:-}" ]; then echo "..."; exit 0; fi`. If a bot-triggered run genuinely needs the value, put it in Dependabot secrets deliberately — do not assume the repository secret carries over.
