# Slack delivery (worked example)

Section 14 of `setup.md` asks you to decide who receives each run — it
deliberately doesn't say how, because that's a real per-deployment choice. This
is one way to answer it: posting the `.xlsx` and `.html` to a Slack channel.

**Quorum has no delivery mechanism built in.** Everything below is code you add
in your own deployment, not something the tool does for you. That is the
boundary from §1: upstream is code you read and never edit; this is a worked
example of something you write on your side.

**Slack API behaviour described here was checked on 2026-09-11.**

## Webhook or bot token — decide this first

| Mode | Can attach files? | Setup |
|---|---|---|
| Incoming Webhook | No — text/blocks only | One URL, no app review |
| Bot token (`chat:write` + `files:write`) | Yes | A Slack app, installed to your workspace |

If you want the actual `.xlsx`/`.html` to land in the channel — not just a
message about them — you need a bot token. A webhook's API has no file-upload
capability at all; this isn't a permissions thing, it's a hard limit of that
API, and it's easy to build the webhook path halfway before hitting it.

## Setting up the bot token

1. Create a Slack app, scoped to `chat:write` and `files:write`, and install it
   to your workspace. This needs Slack workspace-admin access — if you don't
   have it, this is a request to whoever does, the same way the Salesforce
   External Client App in `salesforce-access.md` needs a Salesforce admin.
2. Invite the bot to the channel you want it posting to. Creating the app and
   installing it does not do this by itself — the bot has no channels until you
   add it to one, same as a human user would.
3. Get the channel's ID, not its name. `files_upload_v2` (below) takes the ID
   (e.g. `C0123ABCDEF`), not `#your-channel`.
4. Store the bot token (`xoxb-...`) in whatever secret store your deployment
   already uses for the Salesforce/Gong credentials — never in `.env` for a
   deployed run, for the same reason `salesforce-access.md` gives for
   `SF_CLIENT_SECRET`.

**Before you point this at a channel, decide who is in it.** The files carry
real people's names, titles and CRM state. A channel is easier to join than a
database is to query, and channel membership changes without anyone revisiting
this decision. §14's point about repository access being wider than the set of
people who should read a stakeholder map applies here in exactly the same
shape.

## Adding the dependency

`slack_sdk` isn't one of Quorum's own dependencies — it's something your
deployment needs for this delivery step specifically. How you declare it
depends on how your deployment installs Quorum in the first place: if you
already have a `requirements.txt` or `pyproject.toml` for deployment-owned
code, add it there; if your only install step is `pip install -e .` against
this repo, add a small separate dependency file for your own scripts and
install it as an extra step, so it stays clearly distinguished from Quorum's
own pinned dependencies rather than merged into them.

**If you build your own image, the dependency file and the script are two
separate things to add to it.** Adding a `COPY` for `requirements.txt` does not
put `deliver_to_slack.py` in the image. It is easy to add one and forget the
other, and the failure is neither loud nor early: the build succeeds, the image
looks right, and the delivery step fails at run time with a plain "file not
found" — after the weekly run has already done all its work.

## Finding the files: read the manifest

`quorum weekly` writes `last_run.json` into `OUTPUT_DIR` as its final step —
the output paths and the week they belong to (§12). **Read that.** The
two alternatives both look reasonable and are both traps: rebuilding the
filename pattern yourself couples your script to names upstream can change, and
scraping the `[✓] Wrote …` log lines couples it to log output, which is not an
interface this project versions. Either would break at the *end* of a run,
after every Gong read and every CRM call had already been paid for, and it
would break quietly.

The manifest's presence also means the run finished, so a delivery step that
can't find it should fail rather than post a stale week.

## The script

```python
#!/usr/bin/env python3
"""Post a completed weekly run's output files to Slack.

Reads OUTPUT_DIR/last_run.json to find the run's files.

Env vars required:
    SLACK_BOT_TOKEN   - bot token, xoxb-..., scopes: chat:write, files:write
    SLACK_CHANNEL_ID  - target channel ID (not the #name)
    OUTPUT_DIR        - the same directory quorum weekly writes to
"""
import json
import os
import sys
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

SUPPORTED_MANIFEST_SCHEMA = 1


def load_manifest(output_dir: Path) -> dict:
    path = output_dir / "last_run.json"
    if not path.exists():
        # No manifest means no completed run to deliver. Posting the previous
        # week's files silently would be worse than failing here.
        sys.exit(f"[!] No manifest at {path} — did quorum weekly finish?")

    manifest = json.loads(path.read_text())
    if manifest.get("schema") != SUPPORTED_MANIFEST_SCHEMA:
        sys.exit(
            f"[!] Manifest schema {manifest.get('schema')}, expected "
            f"{SUPPORTED_MANIFEST_SCHEMA} — check the upstream changelog "
            f"before assuming these paths mean what they used to."
        )
    return manifest


def summary_lines(manifest: dict) -> list[str]:
    """One line per tab, zeros and restatements left out — the Summary tab in
    the attached files has every count. A run from a version before the summary
    existed has no `summary` key: post the files without it."""
    if "summary" not in manifest:
        return []
    data = json.loads(Path(manifest["summary"]).read_text())
    stats = {s["key"]: s for s in data["stats"]}
    provider = data.get("enrichment_provider")

    def count(key):
        return stats[key]["count"] if key in stats else None

    def nonzero(key, text):
        n = count(key)
        return f"{n} {text}" if n else None

    def line(label, parts):
        parts = [p for p in parts if p]
        return f"*{label}:* " + " · ".join(parts) if parts else None

    fit = stats.get("companies_fit")
    recent = stats.get("companies_fit_recent_senior")
    days = data.get("recent_days")
    found = stats.get("people_not_in_crm_found")
    not_in_crm = count("people_not_in_crm")

    lines = [
        line("Company coverage", [
            f"{fit['count']} of {fit['out_of']} companies met fit your profile"
            if fit else f"{count('companies_met')} companies met",
            nonzero("companies_not_assessed", "could not be assessed"),
            (f"{recent['count']} have had a senior contact in the last {days} days"
             if days else f"{recent['count']} have had a recent senior contact")
            if recent else None,
        ]),
        line("Met this week", [
            f"{count('people_met')} people",
            (f"{not_in_crm} not in your CRM"
             + (f" ({found['count']} found by {provider})" if found and provider else ""))
            if not_in_crm else None,
            nonzero("people_in_crm_no_title", "CRM records with no title"),
        ]),
        line("Stakeholder list", [
            nonzero("stakeholders", "people"),
            nonzero("stakeholders_no_title", "with no title"),
            nonzero("stakeholders_no_linkedin", "with no LinkedIn"),
            nonzero("stakeholders_no_mobile", "with no mobile"),
        ]),
    ]

    # Largest first; a tie keeps the queue's own order, which sorted() preserves.
    queue = sorted(
        ((s["what"], s["count"]) for s in data["stats"] if s["key"].startswith("queue:")),
        key=lambda kn: -kn[1],
    )
    if provider:
        total = sum(n for _, n in queue)
        lines.append(
            f"*Review queue:* {total} rows to check — "
            + ", ".join(f"{kind} {n}" for kind, n in queue if n)
            if total else "*Review queue:* nothing to check"
        )
    elif "enrichment_provider" in data:
        # Null, not missing: the run knew no provider was configured. A file
        # without the key is from an older version, and says nothing either way.
        lines.append(
            ":warning: Enrichment not configured this run: "
            "no provider lookups, no review queue"
        )

    lines.append("Open the HTML for the full map.")
    return [l for l in lines if l]


def deliver(manifest: dict) -> None:
    token = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL_ID")
    if not token or not channel:
        sys.exit("[!] Missing SLACK_BOT_TOKEN or SLACK_CHANNEL_ID")

    week = manifest["week_start"]
    uploads = []
    for key in ("xlsx", "html"):
        path = Path(manifest[key])
        if not path.exists():
            sys.exit(f"[!] Manifest names a file that is not there: {path}")
        uploads.append({"file": str(path), "title": path.name})

    try:
        WebClient(token=token).files_upload_v2(
            channel=channel,
            initial_comment="\n".join(
                [f"Quorum weekly run — week of {week}"] + summary_lines(manifest)
            ),
            file_uploads=uploads,
        )
    except SlackApiError as e:
        sys.exit(f"[!] Slack delivery failed: {e.response['error']}")


if __name__ == "__main__":
    deliver(load_manifest(Path(os.environ.get("OUTPUT_DIR", "output"))))
```

Titles come from the filenames in the manifest rather than being composed in
the script — same reason as reading the manifest at all. Earlier drafts of this
file composed them by hand and got both names wrong.

The inputs `.json` isn't posted here — it's raw structured data, not something
a person reads in Slack, and it contains every input the run read. Add it as a
third upload if your team wants it, bearing in mind it is the most sensitive of
the files. The summary is posted as text instead: it holds counts only, no
names.

**The message is a digest, not the Summary tab.** One line per tab, with the
counts someone would act on; a zero, or a count that restates another, is left
out, because the Summary tab in the attached files has all of them. A line for
a source your deployment does not configure is absent — the same rule as the
tabs. The one line that reports an absence is the enrichment warning, and it
fires only when `summary_<week>.json` carries `enrichment_provider: null`: the
run knew no provider was configured. A file without that key comes from an
older version and says nothing either way, so it gets no warning. If your
deployment never configures a provider, delete that branch.

## Delivery is not retention

Posting to Slack does not satisfy §14's retention requirement. A channel is a
delivery surface with its own retention policy, usually set by someone else,
and a file posted to it is not a record you can reliably read back a year
later. Decide retention separately, as §14 describes.

## Three things that cost time before

- **A bot with no channel invite fails silently on the first post**, not at
  setup — the app and token both look fine until the first real message.
- **`files_upload_v2` rejects a channel name instantly, unclearly.** If the
  error mentions `channel_not_found` and you passed `#something`, that's the
  name-vs-ID mixup, not a permissions problem.
- **A delivery step that exits non-zero still needs somewhere to be seen.**
  If it runs as a separate step after `quorum weekly`, your runner now has two
  places a failure can happen. See `paas-deployment.md` on platform alerting
  covering service health rather than a run's exit code.
