# Salesforce access

Two ways in. Trying it out by hand means pasting a short-lived token; a deployed
run uses client credentials. **The code prefers a pasted token whenever it finds one**, so a
leftover `SF_ACCESS_TOKEN` silently keeps the two-hour path in use even after the
client-credentials variables are set.

| Mode | Variables | Use |
|---|---|---|
| Pasted token | `SF_ACCESS_TOKEN` + `SF_INSTANCE_URL` | Testing, and trying it out by hand. Expires in ~2 hours. |
| Client credentials | `SF_TOKEN_URL` + `SF_CLIENT_ID` + `SF_CLIENT_SECRET` | Unattended runs. No paste, no refresh. Leave `SF_ACCESS_TOKEN` empty. |

## Token runbook

A per-session ritual, not one-time setup.

1. **Log in** on a machine that can reach Salesforce:
   ```bash
   sf org login web --instance-url https://your-domain.my.salesforce.com
   ```
2. **Reveal the token.** Plain `sf org display` redacts it now; use the flag and
   `--json`:
   ```bash
   SF_TEMP_SHOW_SECRETS=true sf org display -o you@example.com --json
   ```
3. **Paste into `.env`** (git-ignored): `SF_ACCESS_TOKEN` and `SF_INSTANCE_URL`.
4. **On a 401 mid-run, repeat 1–3.** A 401 means "connected, token expired" — it
   is not a network problem.

Two things that cost time before:

- `python-dotenv` does not override an exported variable. If you export a fresh
  token in the shell, that one wins over `.env` — verify which one the process
  actually sees rather than assuming, because the failure surfaces as a wall of
  401s well into a run.
- If you are comparing two runs, both must use the same token. If it expires
  between them, re-run both, not just the second.

## Client credentials

The Connected App (or External Client App) behind this is created by a
Salesforce admin in your own org. It yields `SF_TOKEN_URL`, `SF_CLIENT_ID` and
`SF_CLIENT_SECRET`. Until it exists there is no unattended weekly run.

## Fields dropped from the bench query

The bench query selects the minimum that produces the artifact — every field is
read by a column or by the ranking. Its standard half lives in
`quorum/crm/salesforce.py` (`BENCH_STANDARD`, `CONTACT_STANDARD`); everything
else comes from the deployment's resolved field map, so the query runs against
any Salesforce org whether or not a managed data package is installed. Described
in `docs/pipeline.md`, step 0b.

**This section is the record of what was dropped and why.** It comes from one
real org's bench rather than from documentation; yours will differ.

**Sequence-state custom fields** — an email sent through a sequencing tool is
written back to Salesforce as a Task, and `LastActivityDate` already rolls Tasks
up, so such a field mostly restates what the pipeline has. Where the two
disagreed — contacts showing an `active` sequence with no logged activity —
the sequences were years stale. The field was wrong, not the rollup. And `emailed` versus `contacted`
changes nothing a reader would do with the row.

**An org-local LinkedIn field** — selecting it by name made every bench query
non-portable. In that org it added a URL for a small tail of contacts beyond the
managed-package field, which already covered most of the bench. Not worth the
portability cost for the remainder.

**The field map is how that remainder comes back.** The resolver finds both fields,
ranks the managed-package one first on populated rows and keeps the org-local
one behind it as the fallback; the first populated value wins at read time.
Nothing is selected by a name written in this repository, so the portability
cost that made the trade-off necessary is gone.

**`LastModifiedDate`, `Id`, `AccountId`, `Account.Name`, `Account.Type`** on the
bench query — selected but never read downstream.

A custom field that does not exist raises `INVALID_FIELD` and kills the whole
query rather than returning a blank column, which is why a hardcoded org-local
name is a run that dies at the next customer rather than a cosmetic wart.
