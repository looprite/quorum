# Setting Quorum up

From nothing to a weekly stakeholder map. Written for someone who can run
commands and read an error, most likely working alongside an agent reading this
repository with them.

Read section 1 before anything else. It is the one part that, ignored, makes
everything after it come apart later rather than immediately.

---

## 1. The boundary

Two repositories are involved. Only one of them is yours to change.

**Upstream — this repository.** All of the code. You read it. You never edit it.

**Yours — a repository in your own version control.** Four things, and nothing
else:

| Yours | What it means |
|---|---|
| Configuration | The values that make this deployment yours — your domains, your ICP, your tuning |
| Secret *references* | The names of the secrets, not the secrets. Values live in your secret store. |
| Your deployment pipeline | How the code reaches a machine, and what runs it on a schedule |
| The record of what it produced | Every weekly run, kept — a run cannot be reproduced later. Yours to hold; where you keep it is section 14. |

**Why this matters more than it looks.** The instinct on arriving at a repo you
need to run is to fork it and start editing. A fork can take no update. Six
months later upstream has fixed something you are also hitting, and there is no
path to it that is not a manual merge of code you did not write. That outcome is
what this whole repository is shaped to avoid, which is why it is the first thing
stated rather than a footnote.

**The two things most likely to make you reach for the code are already
configuration.**

- *"Our Salesforce field is called something else."* Nothing in this repository
  names a non-standard Salesforce field. At setup, `quorum init` reads your
  org's own description of `Account` and `Contact`, matches fields against
  generic patterns, counts how many records actually have each one populated,
  and stores the result. Every query is then built from that map.
  `quorum resolve-fields` re-runs it when your admin adds a field.
  See `docs/supported-configuration.md`.
- *"Our ICP is different."* Employee band, HQ geography and the seniority bar
  are the focus profile — arguments to `quorum init`, stored in your database.

If you find something that genuinely requires a code change, that is an upstream
change. Raise it. Do not patch it locally: a local patch is a fork wearing a
smaller hat.

**No contributions are accepted** and pull requests are turned off. This is not
unfriendliness — see `LICENSE` and the README.

---

## 2. What you are setting up

Four things. The first three take minutes; the fourth is the rest of this guide.

1. **A Claude project** — where you and an agent do the setup and, afterwards,
   read the output. First, so the agent is with you for the rest.
2. **A repository in your own version control** — config, secret references,
   your pipeline, and the record of every run. Per section 1.
3. **A Linear project** — where the work and the open questions are tracked.
4. **A running deployment** — a PostgreSQL database, the pipeline installed
   against it, and a schedule that runs it.

**The first three are once per organisation. The fourth is once per
deployment.** That distinction matters twice: if you stand up a second
deployment later — a different region, a separate business unit — you repeat
only sections 7 onward, reusing the project, the repository and the tracker.
And when you come back to upgrade, sections 1 to 6 are already done and you are
looking at section 16.

---

## 3. Before you start

### First: does this apply to you at all?

Three questions. If any answer is no, stop here rather than several sections in.

1. **Is Gong your meeting source?** It is the only importer that exists. No
   Zoom, no Teams, no Fireflies, no Otter.
2. **Is Salesforce your CRM?** HubSpot works as a secondary source of marketing
   contacts, but the reconciliation, the firmographics and the stakeholder bench
   are all read from Salesforce. HubSpot as your *primary* CRM is not supported.
3. **Do you meet the people you want mapped?** The whole pipeline starts from
   recorded meetings. A company nobody has met has no row anywhere in the
   output.

`docs/supported-configuration.md` is the longer version of this, including what
happens at the edges. If you answered no to 1 or 2, that file is the honest
account of what it would take, and nothing in this guide will work in the
meantime.

### Two machines, and they are not the same question

Most of the "wait, where am I supposed to be running this?" confusion in a
setup like this comes from one place: there are two machines involved, they are
usually different, and they get decided at different times.

| | What it is | Decided |
|---|---|---|
| **The workstation** | Where you install from, apply the migrations from, and run the first `init`, `import` and `weekly` by hand. Your own laptop or desktop if it can reach the systems below; otherwise a jump box, a VM or a server inside your network. | Now — sections 8 to 12 |
| **The runner** | Whatever executes the two scheduled jobs afterwards, unattended, every week without you. A server, a container, a scheduled task on your hosting platform. | Later — section 13 |

Ask them separately, and in that order. Everything through section 12 happens
on the workstation, and nothing before section 13 requires you to have picked a
runner. They can turn out to be the same machine; do not assume it.

### What both of them have to reach

The pipeline talks to three outside systems and needs a network path to all
three:

- your **PostgreSQL database** — frequently private to your own network or to
  your hosting platform's,
- **`api.gong.io`**, over HTTPS,
- your **CRM** — your Salesforce org, plus HubSpot if you use one.

That is what decides which machine the workstation can be. A public API like
Gong is reachable from almost anywhere. A private Postgres and a Salesforce org
behind your company's login usually are not, and typically want a machine
inside your own network. A sandboxed environment in particular — the container
an agentic coding tool runs commands in, a hosted notebook, a cloud shell you
did not configure yourself — will often reach `api.gong.io` perfectly well and
fail on the other two, and it fails as a connection timeout rather than as
anything that says "wrong machine". Run the checks in the table below from the
machine you actually intend to use, before you install anything on it.

### How to run the commands in this guide

Every `bash` block below is typed at a shell prompt on the workstation, unless
the text says otherwise.

- **The application.** On macOS that is **Terminal** — Applications → Utilities
  → Terminal, or ⌘-Space and type "Terminal". This guide has been walked
  end to end on macOS only; other platforms are untested here.
- **The directory.** If you have no convention of your own, use **`~/quorum`**
  — a folder named `quorum` in your home directory. Section 8 creates it by
  cloning into it, and every command after that assumes you are inside it.
  `cd ~/quorum` gets you back there in a new terminal.
- **Angle brackets are deleted, not typed.** Where a command or a template
  contains something like `<paste the connection string here>` or
  `<YOUR REPO URL>`, replace the whole thing — brackets and all — with your own
  value. Nothing catches you if you don't: the shell will cheerfully set a
  variable to the literal words `<paste the connection string here>`, report no
  error, and let you discover it several steps later as a connection failure
  that looks like a bad password.
- **An exported variable dies with the terminal window.** `export FOO=…` lasts
  for that one tab. Close it, open a second one, or restart the machine, and it
  is gone — and the commands that needed it fail in ways that never mention it.
  Section 8 says what to do about the one this guide leans on.

### What you need in hand

Everything below is needed. Get them in hand first; discovering a missing one
halfway through step 7 costs more than checking now. Run the checks from the
workstation.

| You need | Why | How to check |
|---|---|---|
| **PostgreSQL 13 or later**, reachable from both machines | The product database. `gen_random_uuid()` is built in from 13. | Reachability first: `nc -z -w5 <host> <port>`. Then the real thing: `psql "<your connection string>" -c "select version();"` |
| **Python 3.11 or later** on the workstation | The pipeline is Python. | `python3 --version` |
| **`psql`** on the workstation | The migrations to apply | `psql --version` |
| **Gong API credentials** — an access key and secret, **read-only** | The meeting source. Everything downstream reads meetings imported from here. | Gong admin → API |
| **Outbound network to `api.gong.io`** from both machines | The overnight job needs it too, not just your workstation | `curl -sI https://api.gong.io` |
| **Salesforce access** — see section 7 | The CRM half of the map: reconciliation, firmographics, the senior contact bench | `curl -sI https://<your-domain>.my.salesforce.com` — a `401` or a redirect is a pass. You are testing the network path, not the credentials; those come in section 7. |
| **A HubSpot private-app key** *(optional)* | Marketing contacts. Absent, the HubSpot columns are left out of the output entirely rather than reported as "no" or as a contact count of 0. | |

**Check reachability before credentials, for the database especially.** `psql`
tests the network path and the password in one shot, and a timeout and a wrong
password produce errors that are easy to confuse — which matters most when the
database sits behind a private network or a VPN, exactly where you are most
likely to be looking at the wrong problem. `nc -z -w5 <host> <port>` needs no
credentials and answers only the first question. If that fails, no connection
string will help until the network path exists.

**What the Gong key needs, and what it must not have.** Read-only, covering
**Calls** — including attendee data, which is the half the whole stakeholder
map is built from — and **Users**. No write scopes: the pipeline never writes
anything back to Gong, so a key that could is a liability with no upside.

> **The Access Key Secret is shown once.** Gong displays it at the moment of
> creation and cannot show it to you again afterwards. There is no "reveal"
> later — if you lose it, you issue a new key. Put it straight into your secret
> store as you create it, not into a note, a message or a chat window
> (section 4).

**Salesforce is effectively required**, though the pipeline runs without it. The
stakeholder list is built entirely from Salesforce contacts, so without it you
get the meeting reconciliation and the company coverage, and a stakeholder tab
that is all gaps.

**It costs nothing to run** unless you turn on the optional enrichment provider,
which spends that provider's credits — `docs/enrichment.md`. Without it, every
field in the output comes from Gong, Salesforce or HubSpot — data you already
own.

---

## 4. Step 1 — The Claude project

**Do this one first.** Once the project points at this repository, everything
after it is done with an agent that has already read this guide sitting
alongside you — including the steps that are fiddlier than they look. That is
the working arrangement section 2 describes and the rest of this document
assumes.

Create a project in Claude. All three fields, ready to fill in and paste:

**Name:** `Quorum — <YOUR COMPANY>`

**Description:** `Our Quorum deployment: weekly stakeholder maps from recorded
meetings, reconciled against Salesforce. Code is read from upstream, never
edited here.`

**Instructions** — this is the text that makes an agent in the project useful
rather than guessing:

```
This project runs a Quorum deployment for <YOUR COMPANY>.

The code is upstream at github.com/looprite/quorum and is read-only to us.
We never edit it, fork it or patch it. Read it there when you need to know how
something behaves. Start with docs/setup.md, then README.md and
docs/supported-configuration.md.

Our repository is <YOUR REPO URL>. It holds only: configuration, references to
secrets, and our deployment pipeline. If a task seems to need a code change,
say so and stop — that is an upstream change, not something we make here.

Our deployment:
- Database: <where>
- Meeting source: Gong
- CRM: Salesforce
- Scheduled by: [TBD — section 13]
- Weekly output lands: [TBD — section 14]

House rules for this project:
- Every number names its source, or says unknown.
- Prefer measuring to reasoning — go and get the number.
- Never present a guess as a fact. Gaps are output, not failure.
- Do not add columns, scores or features that were not asked for.
- Never put a credential in this project or in a conversation. Name where a
  secret lives; never its value.
```

**Move this conversation into the project now**, before the next section: a
conversation started outside the project never gets those instructions, however
good they are. Then add anyone else who will touch this deployment — at minimum
whoever administers your infrastructure, who owns the decision in section 13 and
may be needed in section 7.

**`<YOUR REPO URL>` does not exist yet, and that is fine.** You create it in
step 2; fill the placeholder in then. Everything else in the template you can
fill in now.

**Pointing the project at this repository is just the URL** — it is public.
Nothing to authorise, install or configure. If you are hunting for a settings
screen, there isn't one.

**Do not upload files from this repository into the project.** An uploaded file
is a copy frozen at the moment you uploaded it, so the first time upstream
changes everyone is following a stale document in good faith, with no diff and
no merge path back. That is section 1's fork problem in a different costume. The
project references the repository; it never contains it.

### Secrets do not go into the conversation

You are about to be handed credentials — a database connection string, a Gong
Access Key Secret, a Salesforce client secret, perhaps a HubSpot key. The rule
is here rather than further down because by section 7 it would already be too
late.

- **Never paste one into the chat** — not a connection string, not a token, not
  "just so you can check the format". A conversation is stored and searchable.
- **Run the commands that touch a credential yourself** and report the outcome
  rather than the input: "connected", "eight tables", "401 on the second call".
- **A secret shown once goes straight to your secret store.** Gong's Access Key
  Secret is the case that bites (section 3): displayed at creation, never again.
- **Project instructions and tracker issues name where a secret lives, never
  what it is** — "our secret store, key `QUORUM_DATABASE_URL`". Those texts are
  permanent and visible to everyone in the workspace.
- **If one lands somewhere it shouldn't, rotate it.** Deleting the message is
  not a rotation.

Output carries them too: a traceback or a log line can hold a connection string.
Read what you are about to paste.

### Reading it and running it are different things

| Activity | What it needs |
|---|---|
| **Reading** — how does this behave, what does that flag do, what does `0002` create, why is it shaped this way | The URL. Nothing else. No clone, no credentials, no database. |
| **Executing** — applying the migrations, `quorum init`, `quorum import`, `quorum weekly` | A clone on the workstation of section 3 — a machine that can reach your database, Gong and Salesforce. That is step 5, in section 8. |

Asking what `RECENT_DAYS` does needs nothing but the address, and you can do it
right now. Running the weekly job needs a machine, a database, a virtualenv and
real credentials — and the agent in your Claude project is not on that machine
unless you have put it there.

Add the Linear MCP if your team uses it, so the agent can read the project
below. Nothing in the pipeline requires it.

---

## 5. Step 2 — Your repository

Create an empty repository in your own version control. What goes in it:

```
.env.example        # copied from upstream, filled in with YOUR values —
                    # names and non-secret values only, never a secret
deploy/             # your pipeline: however the code reaches a machine
schedule/           # your scheduled job definitions
runs/               # if you keep runs here — see section 14
README.md           # what this deployment is, who owns it, where the output goes
```

What does **not** go in it: any file from `quorum/`, `migrations/`, `tests/` or
`docs/`. Those are read from upstream. If you find yourself copying one in to
change a line, stop and re-read section 1.

Record in your README which upstream commit or tag you are running. When you
update, that is the thing you move.

Once it exists, put its address into `<YOUR REPO URL>` in your Claude project
instructions from step 1.

---

## 6. Step 3 — The Linear project

Create a project for the deployment. It is where the two questions this guide
cannot answer for you get tracked and closed:

- **What runs the scheduled job** (section 13).
- **Where the finished file lands, and where every run is kept** (section 14).
  The first decides whether anyone reads the output at all. The second decides
  whether you can ever ask what changed.

**Nothing in the pipeline reads your tracker.** No integration, no API call, no
configuration pointing at it. It is where *people* keep the decisions this guide
deliberately does not make for you, and any tracker does that job — Linear is
named because it is what upstream uses, not because anything depends on it.

**If your tracker has an integration for the agent in your project, connect it
now.** Without one, every issue is written by hand and pasted across, which is
the friction that ends with decisions not getting recorded at all — and an
undocumented decision about the runner or the output destination is exactly what
this project exists to prevent.

**Four milestones, if you want them**, matching where this guide actually
stalls: *access and infrastructure* (sections 7–8), *first install and import*
(9–11), *first weekly run* (12), *scheduled and delivered* (13–14). Setup tends
to stop at a boundary between those rather than mid-section, so they are useful
for saying where you are.

---

## 7. Step 4 — Salesforce access

Do this before step 7, not after. `quorum init` resolves your CRM field map by
describing your org, and it can only do that if it can reach Salesforce. Run
`init` with Salesforce unconfigured and you get an account with no field map;
a later weekly run with Salesforce *configured* and no map stops rather than
running — because every query would silently fall back to standard fields only
and the output would be quietly wrong. (`quorum resolve-fields` fixes it, but
it is easier not to get there.)

Two ways in.

| Mode | Variables | Use |
|---|---|---|
| **Client credentials** | `SF_TOKEN_URL`, `SF_CLIENT_ID`, `SF_CLIENT_SECRET` | The deployed run. No paste, no expiry. This is what a scheduled job needs. |
| **Pasted token** | `SF_ACCESS_TOKEN`, `SF_INSTANCE_URL` | Trying it out by hand. Expires in about two hours. |

Client credentials come from an app that a Salesforce admin at your company
creates. Until that exists there is no unattended weekly run.

### What to ask your Salesforce admin for

Ask for it as one request, because it is one:

- **An External Client App** in your Salesforce org, with the **client
  credentials flow** enabled.
- **A dedicated run-as user** for that app — an integration user that exists
  for this and nothing else.
- **Read and describe permission on `Account` and `Contact`** for that user,
  and nothing beyond it. No managed package. No write access. No other object.

That is the entire ask. `SF_CLIENT_ID` and `SF_CLIENT_SECRET` come from the
app; `SF_TOKEN_URL` is your org's OAuth token endpoint.

**The run-as user should not be a person.** Pointing it at somebody's own login
works for testing and will break the schedule. The first time that person
changes their password, loses a permission, has MFA enforced on them or leaves
the company, the weekly run stops — at 3am, attributed to nobody, looking from
the outside like a Salesforce outage. A dedicated integration user is the
difference between a schedule that keeps running and one that quietly stopped
in March.

#### Creating the integration user

> *As observed on a Salesforce org, September 2026.* Same caveat as the section
> below: if what is on your screen disagrees with this, trust the screen.

This is the most error-prone step in the guide, and nearly all of the difficulty
is three things with almost the same name.

**The user licence.** `Salesforce Integration` is an API-only *user* licence —
that user cannot log into the UI at all, which is exactly what you want.
Enterprise editions and above include a small number of them at no extra cost,
so check what you already have before assuming you need to buy one.

**The profile.** `Minimum Access - API Only Integrations` is the matching
profile. Start there and add nothing to it.

**The permission set licence — a different thing with almost the same name.**
The permission set that grants object access needs `Salesforce API Integration`,
which is a *permission set* licence, not the user licence above. Choosing
`--None--` lets you build the permission set successfully and then fails at
**assignment**, with:

```
The user license doesn't allow the permission: Read Account
```

That message sends you looking at object permissions, which are fine. The cause
is a licence field two screens back.

**A permission set's licence cannot be changed after it is created.** It is not
in Edit Properties. If you pick the wrong one, delete the permission set and
build it again.

What the permission set should grant: **Read**, **View All Records** and **View
All Fields** on `Account` and `Contact`, and nothing else. View All Fields
rather than ticking fields one by one, because the field map is resolved
dynamically at setup — which fields it will read cannot be enumerated in
advance.

Four smaller things, each of which costs time:

1. **The username must be globally unique across all of Salesforce, and does not
   have to be a real address.** A suffixed form such as
   `quorum-integration@yourcompany.com.prod` works. The *email* field is
   separate and can be an existing monitored inbox.
2. **Select objects by API name.** The object list is full of similar display
   names; `Account` and `Contact` are unambiguous.
3. **`Run As (Username)` wants the username**, not the display name the picker
   offers you. The display name is rejected.
4. **The assignment screen may arrive empty**, with a message about Salesforce
   Classic. There are two list views both named "All Users", and only one of
   them works.

#### Finding it in the Salesforce UI

> *As observed on a Salesforce org, August 2026.* Salesforce moves this
> interface, and a confidently wrong instruction is worse than none: if what is
> on your screen does not match what is below, trust the screen.

**"Connected App" and "External Client App" are not two names for one thing.**
External Client Apps are the current mechanism. On a current org, Setup → App
Manager offers **New External Client App** and there is no "New Connected App"
button at all. Older orgs may still show Connected Apps, and an existing
Connected App keeps working — but if you are creating one today you are
creating an External Client App, and any instructions written for Connected
Apps will not match what you see.

Three things inside that flow cost real navigation time:

1. **The configuration is split across tabs.** Once the app exists, its
   settings are not one page: **Settings** and **Policies** are separate tabs
   and you need both.
2. **"Enable Client Credentials Flow" appears twice, and only the second one
   counts.** There is a checkbox by that name in the app creation form, under
   the OAuth settings — and another by the same name on the **Policies** tab
   afterwards. Ticking the first does not tick the second. **Only the one on
   Policies activates the flow.** Nothing on screen signals this: the app looks
   configured, and the token request fails.
3. **Run As User is inside a collapsed subsection.** On the Policies tab it
   sits under **OAuth Policies**, which is collapsed by default — not at the
   top level of the page. Expand it and set the user. The flow does not work
   without it.

> **The one that catches people.** The code prefers a pasted token whenever it
> finds one. A stale `SF_ACCESS_TOKEN` left in your environment silently keeps
> the two-hour path in use even after the client-credentials variables are set —
> and the failure surfaces as a wall of 401s well into a run, not at the start.
> Leave `SF_ACCESS_TOKEN` and `SF_INSTANCE_URL` empty for a deployed run.

The pasted-token runbook is in `docs/salesforce-access.md`.

---

## 8. Step 5 — The code and the database

**This is workstation work.** Everything in this section runs in a terminal on
the machine from section 3 — the one with network reach to your database,
`api.gong.io` and Salesforce. The runner that executes the schedule is a
separate decision and comes later, in section 13; you do not need to have
chosen it yet, and nothing here depends on it.

```bash
cd ~
git clone https://github.com/looprite/quorum.git quorum
cd ~/quorum
python3.12 -m venv .venv && source .venv/bin/activate   # any 3.11+ will do
python -m pip install --upgrade pip
pip install -e '.[dev]'
```

`~/quorum` is the default this guide uses; substitute your own location if you
have one, and read `~/quorum` as "wherever you cloned it" from here on. Note
that `source .venv/bin/activate`, like an `export`, applies to that terminal
window only — a new tab needs it again before `quorum` is on the path.

**Name the interpreter, and upgrade pip inside the venv.** Both lines are load-
bearing on a stock Mac. `python3` there is 3.9 — section 3 had you check exactly
that — and a 3.9 venv carries pip 21.2.4, which predates the standard this
project's editable install needs. The failure does not mention Python or pip; it
says `File "setup.py" or "setup.cfg" not found`, which sends you looking for a
missing file that is not supposed to exist. Upgrading pip first is what turns
that into a working install or an honest "requires a different Python" message.

**If `python3.12` is not there at all**, you get `command not found` — a stock
Mac is exactly where a newer interpreter is least likely to already be present,
so fixing one missing-file error by naming the interpreter just produces a
different one. Install a 3.11+ interpreter through whatever package manager you
use (on macOS, Homebrew: `brew install python@3.12`), then run the venv line
again. Do not fall back to bare `python3` to get past it — that is the 3.9 this
paragraph is about, and it fails later and less clearly.

**Why `.[dev]` and not `.`** — `python-dotenv` lives in the `dev` extra, and it
is what reads the `.env` file you are about to write in section 9. Install with
a bare `pip install -e .` and there is no `.env` support at all: the file is
read by nothing, and the first symptom is section 10 reporting `DATABASE_URL`
and `ACCOUNT_DOMAIN` missing while both sit correctly in the file. A deployed
run is the case that genuinely wants the bare install — it takes its values
from your environment's secret store and never has a `.env` to read.

That gives you the `quorum` command. Check it:

```bash
quorum --help
```

### Create the database

The pipeline wants a database of its own. Making one is two moves: get an admin
connection to your PostgreSQL instance, then `CREATE DATABASE`.

**Find the admin connection string, and tell it apart from an app-scoped one.**
A managed Postgres typically shows several strings for the same instance, and
they are not interchangeable. Take the **public** hostname, not the internal one
that resolves only inside the platform's network, and the **admin** credential
for the instance rather than one scoped to an existing database.

Then create it:

```bash
export ADMIN_DATABASE_URL="<paste the admin connection string here>"
psql "$ADMIN_DATABASE_URL" -c "CREATE DATABASE quorum;"
```

The angle brackets go too (section 3). Run that line verbatim and the export
succeeds, setting the variable to the literal text, and the failure arrives one
command later looking like a broken database.

> **`permission denied to create database`.** The common case, not an edge
> case: a string handed to you for an existing database belongs to that
> database's own user, who has no cluster-wide right to create one, and nothing
> you can type from that connection fixes it. You need a superuser credential
> for the instance, or whoever administers it to run the `CREATE DATABASE` and
> hand back a string scoped to the new database.

**Why it wants a database to itself.** The core migrations create their tables
under plain, generic names — `accounts`, `meetings`, `attendees` and the rest.
Point them at a database your business already uses and one of those names is
likely taken. Nothing is destroyed, because `CREATE TABLE` refuses rather than
overwrites, but the migration stops partway and leaves a half-applied schema
whose cause is invisible unless you knew which names to watch.

Now set the connection string the rest of this guide uses. This one is
app-scoped: it names the new database, not the instance.

```bash
export DATABASE_URL="postgresql://user:password@host:5432/quorum"
```

> **This lasts until you close the terminal**, like the venv above. In a fresh
> tab it is simply unset, and `psql` falls back to a local socket:
> `could not connect to server: No such file or directory ... /tmp/.s.PGSQL.5432`
> — an error that names a socket and never mentions the variable. Re-run the
> `export` in each terminal, or put the value in the `.env` file of section 9,
> which `quorum` reads on every run. `psql` does not read `.env`, so the
> migrations below need the export either way.

### If you must share a database: a dedicated schema

A dedicated database is the recommendation, and what the rest of this guide
assumes. If your organisation's convention is one database with a schema per
application, that works too, with no code change: the pipeline emits
unqualified table names, so it uses whatever schema the search path resolves
to.

```sql
CREATE SCHEMA quorum;
```

**Set the search path on the connecting role — never as a connection-string
parameter.** This is the important half, and the reason this is the alternative
rather than the recommendation:

```sql
ALTER ROLE quorum_app SET search_path = quorum;
```

A search path passed in the connection string (`options=-csearch_path=…` and
its variants) fails *silently* when it is absent, misspelled or stripped — and
connection poolers do strip it. The connection still succeeds. The queries
still run. They just run against the default schema, creating or reading the
wrong tables, and nothing anywhere distinguishes that from working. Set on the
role, the path is a property of the credential and travels with every
connection it makes.

`quorum` above is just this guide's example name — the pipeline emits
unqualified table names and never hardcodes a schema, so an existing
deployment keeps whatever schema name it already set up; there's nothing to
migrate.

Confirm it on the connection you are actually going to use, before you migrate:

```bash
psql "$DATABASE_URL" -c "show search_path;"
```

### Apply the migrations

**In filename order:**

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0001_core.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0002_identity.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0003_focus_profile.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0004_crm_field_map.sql
```

They create empty tables and nothing else. `ON_ERROR_STOP=1` matters: without
it `psql` carries on past a failed statement and leaves you a half-applied
schema that looks fine.

**`migrations/0005_run_outputs.sql` is not in that list.** It is optional — the
table it creates is for keeping every weekly run in the database, a decision
made in section 14, not something a run needs to start. Apply it there, if you
choose it.

Verify:

```bash
psql "$DATABASE_URL" -c "\dt"
```

You should see eight tables: `accounts`, `meetings`, `attendees`, `people`,
`person_identifiers`, `person_attendees`, `user_focus_profiles` and
`crm_field_maps`.

Migrations are append-only once applied — your database is yours, not ours, and
cannot be rebuilt from scratch to accommodate an edit.

### Checking the install

The test suite is the one way to confirm the code works on your machine before
you point it at live systems. It needs the `dev` extra, which you installed
above:

```bash
pytest
```

Some of them will skip, and the skips matter more than they look: those are the
tests that need a real PostgreSQL, and they skip silently when they cannot find
one. A run that skips them is green on any machine with no database — which is
to say green almost everywhere, including where something is genuinely broken.

To run all of them, point `QUORUM_TEST_DSN` at a Postgres and run it again:

```bash
export QUORUM_TEST_DSN=postgresql://postgres@localhost:5432/postgres
pytest
```

**Zero failed and zero skipped** is the gate. pytest prints both counts on every
run, so you do not need a number from this guide to check it. Any failure, and
any remaining skip, is worth stopping for.

> **Not your product database.** The suite creates and drops its own databases
> on whatever server that DSN names. Give it a scratch Postgres — a container is
> ideal — never the deployment you set up above.

---

## 9. Step 6 — Configuration

Copy `.env.example` to `.env` and fill it in. `.env` is git-ignored. In a
deployed run these values come from your environment's secret store instead —
same names, no file.

The four that a run cannot start without:

```bash
DATABASE_URL=postgresql://user:password@host:5432/quorum
ACCOUNT_DOMAIN=acme.com          # your own primary domain; names the account row
GONG_ACCESS_KEY=
GONG_ACCESS_KEY_SECRET=
```

Then Salesforce per section 7, and `HUBSPOT_SERVICE_KEY` if you have one.
Enrichment is optional and off unless its key is set; `docs/enrichment.md` has
the variable, what it looks up and what it costs. Leave it for after the first
run works without it.

`.env.example` lists both Salesforce auth modes together. Only one of them
applies to you, and nothing in the file says which — so mark it as you edit:

```bash
# Client credentials — the mode a scheduled run uses.
SF_TOKEN_URL=
SF_CLIENT_ID=
SF_CLIENT_SECRET=

# For a deployed or scheduled run, leave these two blank — see the
# client-credentials mode above. The code prefers a pasted token whenever it
# finds one, so a leftover value here silently keeps the two-hour path in use.
SF_ACCESS_TOKEN=
SF_INSTANCE_URL=
```

Everything else has a default and can be left alone until you have seen an
output and want to change something:

| Variable | Default | What it does |
|---|---|---|
| `RECENT_DAYS` | 90 | How far back still counts as recent contact — **and** how far back `quorum import` reaches with no dates given. One number deliberately: importing less than the recency window puts "no" next to people you met inside it. |
| `SHORTLIST_SIZE` | 3 | People per company on the stakeholder list. The cap is a feature. |
| `GROUP_CALL_MIN` | 8 | Above this many external attendees, a meeting is labelled a group call on the row — so a training webinar does not read as a relationship. |
| `WEEK_START` | current week | Monday of the target week, `YYYY-MM-DD`. Unset, it means the week **containing today** — which is not the same as last week, and is the trap section 13 exists to warn about. Read §13 before scheduling anything. |
| `TZ_OFFSET` | `-04` | The offset the week boundaries are cut on. |
| `OUTPUT_DIR` | `output` | Where a run's output lands — the workbook, the JSON dump, the summary, the HTML view and `last_run.json`. |
| `CUSTOMER_ACCOUNT_TYPES` | empty | Substrings of `Account.Type` marking an existing customer. Empty means the gate is off and ICP fit is employee band plus HQ geography only. Leave it off unless your `Type` field is genuinely maintained as a lifecycle field. |
| `RETAIN_RUNS` | `false` | Store this run's `.xlsx`, `.json` and `.html` into `run_outputs` as the last step of `quorum weekly`. Off by default — a deployment that has not chosen retention must not silently start storing contact data. Section 14 has the migration and the grant this needs before turning it on. |

> **`python-dotenv` does not override an exported variable.** If you export
> something in your shell, that one wins over `.env`. Verify which one the
> process actually sees rather than assuming.

---

## 10. Step 7 — Initialise

Two rows have to exist before anything works: your account, and an active focus
profile. `quorum init` creates both, and resolves the CRM field map while it is
there.

```bash
quorum init \
  --internal-domains acme.com,acme.io \
  --employee-min 200 --employee-max 10000 \
  --geographies "North America" \
  --seniority c-level,vp,director
```

**`--internal-domains`** — your own email domains, and how a colleague is told
from someone at the other company. Give the part after the `@`. Get this wrong
and every one of your own people appears as an external attendee, every week.

**`--employee-min` / `--employee-max`** — the ICP employee band. A company with
no employee count in your CRM fails the test with "no size" rather than passing
on the benefit of the doubt.

**`--geographies`** — HQ geography. A bare value is a region: `North America`,
`EMEA`, `APAC`. Prefix with `country:` to name one country —
`--geographies "North America,country:Germany"`. Common aliases resolve, so
`United States`, `USA` and `US` are the same place. A region or country the
pipeline does not recognise is **refused here**, not warned about — it used to
be accepted, apply no geography filter at all, and report every company met as a
fit.

**`--seniority`** — the bar a CRM title has to clear to reach the stakeholder
list. Known levels: `c-level`, `cxo`, `cro`, `vp`, `director`, `founder`,
`manager`. An unrecognised level is matched against the title verbatim, so a
typo matches nobody and every company's bench comes back empty. `init` warns
about that rather than letting you find it in an artifact.

What you should see, **with Salesforce configured**: three `[✓]` lines — the
account, the focus profile, and the field map with the fields it resolved and
the percentage of records populating each. Read that third block. It is the only
place you will see which field the pipeline decided means "employee count" in
your org, and whether that was right.

**Two field names joined by an arrow — `NumberOfEmployees → Headcount__c` — is a
fallback chain, not a replacement.** It means: try the first; if that record's
value is blank, fall back to the next. Both fields are live, and a company can
be resolved by either. Read it as "A, or B where A is empty" rather than "A was
renamed to B".

**Without Salesforce configured** you get two `[✓]` and one `[i]`, and that is a
successful run, not a failed one:

```
[✓] Account acme.com created · internal domains: acme.com, acme.io
[✓] Focus profile v1 created · 200-10000 employees · NA · c-level, vp, director
[i] Salesforce not configured — no field map resolved.
[*] Next: quorum import
```

The `[i]` is the third block telling you it had nothing to resolve against. The
account and the profile are written and `quorum import` will run. What you do
not have is a field map, and section 7 is the reason to care: configure
Salesforce later and the weekly run will stop until you have run
`quorum resolve-fields`.

**Re-running it** with the same arguments writes nothing. With a *different*
profile it refuses: the profile decides which companies appear on the map, so
changing it is `--replace`, which supersedes the active version and keeps the
old one, inactive, so last week's output stays explicable. Adding a domain later
needs no `--replace` — same profile arguments, one more domain.

---

## 11. Step 8 — Import your history

```bash
quorum import
```

With no arguments this imports the last `RECENT_DAYS` days. It prints how many
calls it found and how long it took — those two numbers are what you extrapolate
a longer backfill from.

For real history, extend the range:

```bash
quorum import --from 2025-10-01 --to 2026-08-24
```

Import at least `RECENT_DAYS` of history before your first weekly run. That
setting decides whether the "Recent contact?" column says yes or no; importing
less produces rows saying "no" for people you actually met.

**Re-importing is safe.** Every write is guarded, and a test asserts that
running the same range twice leaves every table count unchanged. A backfill and
an overnight run *will* overlap, and that is fine.

---

## 12. Step 9 — The first weekly run

```bash
quorum weekly
```

Or for a specific week:

```bash
WEEK_START=2026-08-17 quorum weekly
```

It writes into `OUTPUT_DIR` and nowhere else — no writes back to Gong,
Salesforce, HubSpot or anywhere in your CRM:

- `weekly_stakeholder_map_<week>.xlsx` — the artifact: a Summary tab and three
  numbered tabs (four with an enrichment provider configured)
- `stakeholder_inputs_<week>.json` — every input the run read
- `summary_<week>.json` — the Summary tab's counts, for a delivery step to post
- `weekly_view_<week>.html` — a single-page view of the same thing
- `last_run.json` — the manifest: the four paths above, plus the week they
  belong to. Overwritten by each run.

**The HTML is rendered from the `.xlsx`, not generated alongside it.** The run
writes the workbook first and then reads it back to build the view, so the two
cannot disagree — but it also means the `.xlsx` has to exist, at that path, at
that moment. If your deployment moves or cleans up output files as part of the
run, do it after all five are written, not between them.

**Opening them.** `open <your OUTPUT_DIR>/` shows the folder, or open one
directly — `open <your OUTPUT_DIR>/weekly_stakeholder_map_<week>.xlsx` for the
workbook, or the `.html` for the same thing in a browser without Excel. Obvious
if you live in a file manager; less so after eleven sections of terminal.

**The manifest is the supported way for anything downstream to find a run's
files.** Whatever you write in section 14 to deliver or archive them should
read it rather than rebuilding the filename pattern or parsing the
`[✓] Wrote …` lines — those are not a versioned interface, and a change to
either would break your delivery step silently, at the end of a run, after
every Gong and CRM call had already been paid for. Its `schema` field is there
so a future change to its shape is something you can detect:

```json
{
  "html": "/srv/quorum/output/weekly_view_2026-08-17.html",
  "json": "/srv/quorum/output/stakeholder_inputs_2026-08-17.json",
  "schema": 1,
  "summary": "/srv/quorum/output/summary_2026-08-17.json",
  "week_start": "2026-08-17",
  "xlsx": "/srv/quorum/output/weekly_stakeholder_map_2026-08-17.xlsx"
}
```

Paths are absolute, so a delivery step does not have to share the run's working
directory. The manifest is written last, after retention — so if it is there,
the run finished.

**`summary` is newer than the manifest, and did not bump `schema`.** A key a
reader does not know is one it can ignore, so a delivery step checking for
schema 1 keeps working. The other direction is the one to handle: a deployment
pinned to a version from before the summary has no `summary` key, whatever this
guide says. A delivery step that uses it should treat it as optional.

`summary_<week>.json` holds `schema`, `week_start` and `stats` — a list in the
order the Summary tab shows them, each with a stable `key`, the `what` a person
reads, its `count`, and `out_of` (null where a count has no denominator). A
count whose source was not configured is absent, not zero. The same list is in
the inputs dump under `summary`, which is the copy retention keeps.

The tabs, named here exactly as the workbook names them:

| Tab | What it answers |
|---|---|
| **`Summary`** | The run in counts, each out of its denominator: companies met and how many fit your profile; of those, how many had someone senior contacted recently; people met, how many are not in your CRM, and how many CRM records lack a title, LinkedIn or mobile; the same gaps on the stakeholder list; and, with a provider, the review queue by kind. Each count is a count of rows on the tab it names |
| **`1 - Met this week`** | Who attended from outside, one row per person: whether each is in your CRM, and for those who are, their CRM title, LinkedIn and whether a mobile number is on file. People not in the CRM are listed first, with `—` in the CRM columns — there is no record to read. Attendees with neither email nor domain (meeting bots) are listed at the foot — suppressed visibly, not dropped. |
| **`2 - Company coverage`** | Every external company met: size, HQ, whether it meets your profile, how many contacts you hold |
| **`3 - Stakeholder list`** | The map. The senior people in your CRM at the ICP-fit companies worth considering, capped at `SHORTLIST_SIZE` each. Its caption states the rule in your profile's terms |
| **`4 - Review queue`** | Only with an enrichment provider configured: where your CRM and the provider disagree, for a person to settle. Its `Company` column is the CRM account's name, falling back to the domain only where no account matched — the other tabs key on the domain, so a script filtering this one by domain will miss rows. The provider's values also appear beside the CRM's on tabs 1, 2 and 3 — `docs/enrichment.md` |

**`docs/reading-your-first-run.md` is what to send to whoever receives this
file.** It covers what to check first to know the run worked, what looks
alarming and is not, and what a person does about each kind of gap — written
for the reader of the map rather than for whoever stood the deployment up.

**Things in the output that are deliberate, not defects.** Read
`docs/supported-configuration.md` before concluding anything is broken:

- A company with no employee count is **excluded**, not given the benefit of the
  doubt.
- `HQ unknown` and `HQ not in region` are different rows. One is missing data,
  the other is a decision your profile made.
- A company with no senior CRM contact gets an explicit
  `— no senior contact in Salesforce —` row rather than being left out.
- Mobile numbers are reported as present or absent, never revealed or stored.
- **No action is suggested per person.** The output says who is worth
  considering and stops.
- **Nobody is checked for still being at the company** unless an enrichment
  provider is configured; with one, `Still at company?` on tab 3 gives the
  provider's view, which can be stale too.

---

## 13. Step 10 — Schedule it

**This repository defines no packaging and no scheduling.** That is deliberate:
what runs a scheduled job is different in every environment, and guessing yours
would be worse than saying so.

Two jobs:

| Job | Command | When |
|---|---|---|
| Overnight import | `quorum import --yesterday` | Daily, after your calls have finished syncing to Gong |
| Weekly run | `quorum weekly` | **After the target week's meetings have happened** — Friday evening or Saturday, not Monday morning. Read the next paragraph before choosing. |

### The week it reports on is the week you are in

**This is the one scheduling decision that fails silently.** With no
`WEEK_START` set, a run covers the Monday-to-Monday window *containing today*.
Schedule it for Monday 07:00 and that window is seven hours old: the run
succeeds, exits zero, reports no error, and writes an artifact with headers and
no rows. Every check you have says it worked.

Two shapes are safe. **Run late in the target week** — Friday evening or
Saturday, once Monday to Friday has actually happened. Or **run after the week
closes with `WEEK_START` set explicitly** to that Monday, which is the only way
to report a week you are no longer in.

The shape that looks most obvious is the broken one: scheduling Monday morning
to pick up the week just gone. There is no "last week" default to fall back on —
by Monday the default has already rolled forward, so you report a week that has
barely started rather than the one you meant.

`quorum weekly` warns when the window it is about to use has not closed, naming
how much of it has elapsed. That is a warning and not a refusal, because running
mid-week deliberately is a reasonable thing to want. On a schedule, treat it as
the schedule being wrong.

What the runner needs: the repository at a known commit, the Python environment,
the environment variables from your secret store, outbound network to
`api.gong.io` and your CRM, and network to your database.

Look first at whatever already runs alongside your database — if that platform
offers scheduled tasks, it is the shortest path and the credentials are already
near it.

**If that platform is a PaaS, `docs/paas-deployment.md` is a worked example.**
It covers the decision that determines everything else — whether your job stays
running and is exec'd into, or is started fresh and expected to exit — because
building for the wrong one produces a confusing failure either way. It also
covers what your platform's alerting does and does not cover, which is the
paragraph below.

**A failed run does not currently retry.** Connection errors are not retried,
deliberately: what should happen when a run fails is a decision about your
schedule, not about the code. Decide it when you decide the runner, and make
sure a failure is visible to a person. The rest of this section is how.

### Making a failed run visible

A run that fails loudly is the easy case. The one to design for is the run that
stops happening — no file, no error, no exit code, and nobody watching a
schedule they assume is working. So the check has to be something that goes
stale on its own.

`quorum weekly` writes `last_run.json` last, after everything else. Its presence
means the run finished. So the check is: **is there a manifest for the week I
expected?** One check, no new code, and it catches the job never starting.

**Check the `week_start` value, not the file.** A failed run leaves the previous
run's manifest sitting in place, so "the file is there" stays true forever.

What counts as healthy depends on which schedule you chose above:

| Your schedule | Healthy `week_start` |
|---|---|
| Friday or Saturday, no `WEEK_START` | the Monday of the week you are in |
| After the week closes, `WEEK_START` set | the Monday you set — the previous week |

They differ by exactly one week. Check against the wrong row and a working
deployment reports a failure every time.

**If your platform tears the container down after each run** — the second model
in `docs/paas-deployment.md` — `OUTPUT_DIR` goes with it and there is no
manifest to find. Put it on a volume that persists, or have the run report
somewhere that does.

Whatever runs the check, make sure its result reaches a person.

---

## 14. Step 11 — Where the file lands, and where it is kept

Two questions, and they are not the same one. Decide both before you turn the
schedule on, and write both down in your Linear project.

### Who receives it

An `.xlsx` in an output directory on a server is not a delivery. Somebody has to
receive it — a person, a channel, a shared drive. This is the step that decides
whether the map gets read, and it is the one most likely to be left until later
and then never done.

**`docs/slack-delivery.md` is a worked example of one answer** — posting the
files to a Slack channel — including the decision that catches people out
first, which is that an incoming webhook cannot attach a file at all.

Whatever you choose, **have it read `last_run.json`** (section 12) to find the
files rather than rebuilding their names or parsing the run's log. And decide
who can see wherever you send it: these files carry real people's names, titles
and CRM state, and a delivery surface's membership usually changes without
anyone revisiting this choice.

### Where every run is kept

Delivery is not retention: a file sent to a person is not a record you can go
back to.

**A weekly run cannot be reproduced.** Your meetings, attendees and people
accumulate in your database and re-importing is safe, so that part is always
recoverable. The rest of the run is not. Company coverage, the ICP verdicts and
the stakeholder list are computed against your CRM *as it stood that week*, and
none of it is written back to the database. Re-run last week's window a month
from now and you get the same meetings reconciled against a CRM that has since
changed — a new map with an old date on it, not the map you produced.

So a run that is not kept is gone, and a year of runs answers what one cannot:
who is new at this company, whose title changed, which coverage gap closed,
which company went quiet. Keep all three files from every run — the `.xlsx`,
the `.json` and the `.html`.

Keep them somewhere private, readable back later, and under access you
control. "Readable back" does not mean a live connection from wherever someone
happens to be asking — a short script that loads credentials and queries on
demand satisfies it exactly as a file on disk does.

Two places meet that. One is already part of your deployment; the other you
created in step 2. This guide does not pick for you, but it does have a view
about which is less work, and it is not the one people reach for first.

**Neither of them is the directory you cloned this repository into.** `output/`
is in the upstream `.gitignore` deliberately, because those files hold real
contact data. That holds wherever they end up: the record goes somewhere you
chose and control, never into a clone of an upstream repository.

#### Option A — your database

You already have one. Section 8 had you provision PostgreSQL for the pipeline's
own schema, so this adds a table rather than a system — and, the part most
easily missed, **no new write identity.**

That is the practical argument. An unattended job that commits to version
control needs a credential *into* version control: a deploy key or token to
create, store where the runner can read it, and rotate. The pipeline already
holds database credentials, because it cannot run without them. And that
credential may not be yours to issue — on some platforms the token type that
can write is disabled organisation-wide and the remaining one carries no write
scope, neither of which is visible from the repository's own settings page.
Worth finding out before you choose, because it is not a question of effort.

Two smaller differences. These are binary files, so a repository stores each
week's copy whole and never compacts it — a few megabytes a year, which only
grows. And a table can be asked "which weeks do we have" in one line of SQL,
where a repository needs a checkout. Nothing in Quorum reads `run_outputs`
back; that SQL is yours to write.

Three steps.

**Apply migration 0005:**

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0005_run_outputs.sql
```

It creates one table, `run_outputs`, holding all three files from every run.
The migration file's own header says why: no `account_id` (one deployment
serves one account, so there is nothing here to scope against), and why
`run_date` is written by the pipeline from the run's week start rather than
inferred from today — storage can happen well after the run it belongs to, and
a row dated by when it was stored is a row you cannot line up against
anything.

**Grant the pipeline's role `INSERT` and `SELECT` on it, and nothing else:**

```sql
GRANT INSERT, SELECT ON run_outputs TO <the role your pipeline connects as>;
```

No `UPDATE`, no `DELETE`. That makes the history append-only by privilege
rather than by good intentions — which is the property people are usually
reaching for when they reach for version control.

**Then set `RETAIN_RUNS=true`** in your `.env` or your environment's secret
store (section 9). It defaults off — a deployment that has not made this
choice must not silently start storing contact data. Once it is on,
`quorum weekly` writes all three files into `run_outputs` in a single
transaction as its last step, so a partial failure leaves no rows rather than
one file and the appearance of a stored run; it logs one line either way, so a
run's log always says which case you were in — what it stored, or that
retention is off.

Append-only means a re-run adds a row rather than replacing one. That is
correct — both attempts really happened — so read the most recent `created_at`
for a given `run_date` rather than assuming one row per file per week.

**Test the insert and the read as the pipeline's own role, with the credentials
the scheduled job will use — not the login you created the table with.** A
grant that reads correctly on the page can still be wrong for the identity that
runs unattended, and the first time you find that out should not be a failed
run at 3am that nobody is watching.

#### Option B — the repository from step 2

Also valid. It keeps the record beside your deployment config rather than
inside the pipeline's own database, `runs/` is already in the layout section 5
suggested, and if the `.json` dumps are what you will actually compare later, a
repository diffs those natively.

**The cost is a storage step you own.** Option A is written for you; here you
write and schedule the commit yourself, next to whatever runs the weekly job,
using the write identity discussed above.

The thing to weigh is that **git history is permanent by design**, which is what
makes it a good record and why this should be a decision rather than a default.
A commit cannot practically be un-made, and repository access at most companies
is wider than the set of people who should be reading a stakeholder map. For
many deployments that is a fair trade: it is your own private repository, and
the same data sits in your CRM under access no narrower. If your organisation
has a process for removing an individual's data on request, it is the wrong
trade, and Option A is the safer default.

#### Durability is not automatic either way

Permanence is not durability. Git history is permanent only on the machines
holding a copy, and a database is durable exactly as far as its backups and
point-in-time recovery reach — a question about your hosting rather than about
PostgreSQL. Find out what the answer is for your setup before relying on it.

---

## 15. When it goes wrong

| What you see | What it is |
|---|---|
| `[!] Missing environment: DATABASE_URL, ACCOUNT_DOMAIN` | **Check first: are they set in a `.env`, and did you install with `pip install -e .` rather than `pip install -e '.[dev]'`?** Without the extra, `python-dotenv` is absent and the whole file is ignored, so correctly-set values are reported missing. `quorum` now warns about this on stderr when a `.env` is present. Otherwise: those two are required before anything runs, and an exported shell variable beats the file. |
| `[!] The schema is not there. Apply the migrations first` | Section 8. |
| `bad interpreter: no such file or directory` from `quorum` or `pytest` | A virtualenv hardcodes its own absolute path, so renaming or moving the directory breaks every console script in `.venv/bin`. Delete `.venv` and rebuild it where the directory now lives. |
| `ERROR: File "setup.py" or "setup.cfg" not found. Directory cannot be installed in editable mode` (with `(A "pyproject.toml" file was found, but editable mode currently requires a setuptools-based build.)`) | Your `pip` is too old for an editable install, which almost always means the venv was built by a too-old Python. Nothing is missing from the repository — there is deliberately no `setup.py`. `python -m pip --version` and `python -V` inside the venv; if Python is below 3.11, rebuild the venv with an explicit `python3.12 -m venv .venv`. Section 8. |
| `[!] No account named '<x>'. Run 'quorum init' first.` | `ACCOUNT_DOMAIN` does not match any account row — either you skipped `init`, or the value changed since. |
| `accounts.internal_domains is empty` | `init` did not run, or ran without `--internal-domains`. Every attendee would be classified external. |
| A run stops before doing anything, complaining about the focus profile | There is no active profile. This is a hard error on purpose: an absent profile makes the ICP test pass everything, and the output would look entirely normal and be wrong. |
| A run stops complaining about the field map | Salesforce is configured but no map is stored. `quorum resolve-fields`. |
| A run stops complaining that `run_outputs` does not exist | `RETAIN_RUNS` is on but migration `0005_run_outputs.sql` has not been applied. Apply it and grant the pipeline's role `INSERT` and `SELECT` on it — section 14. |
| `[!] Gong credentials not configured` | `GONG_ACCESS_KEY` / `GONG_ACCESS_KEY_SECRET`. |
| A wall of `401`s partway through a run | An expired pasted Salesforce token. It means "connected, token expired", not a network problem. Section 7 — and check whether an exported shell variable is winning over your `.env`. |
| A column reads `not available in this CRM` | The field map resolved nothing for it. Expected, not a failure. `quorum resolve-fields` after your admin adds a field. |
| The employee-count or LinkedIn field looks like it picked the wrong one | Re-read the field-map block from `init`, or run `quorum resolve-fields`, which prints every candidate with its populated percentage and every rejection with the rule that rejected it. Counting tells you which field has data, not which field means what you want. |

**Never print a credential** — and never paste one into a conversation with an
agent. The full rule is in section 4, which is where you should already have
read it. The trap specific to this section: a failing test that constructs a
real config will load your environment, so a traceback can carry a live secret
in it. Build test clients from stubs, and read a traceback before you paste it
anywhere.

---

## 16. Keeping up to date

Upstream moves. You take updates by moving to a newer upstream commit or tag —
`git pull`, or whatever your pipeline does to fetch the code — and re-running
any new migrations. Nothing else, because nothing else is yours.

**If your deployment pins a commit, nothing upstream reaches you until someone
changes the pin.** That is the point of pinning and it is the right default —
but it means "upstream added X" and "this deployment has X" are two different
statements, and the gap between them is however long since the last bump. A
deployment that pins is a deployment where somebody has to decide to update.

The failure this produces is worth recognising, because it does not look like a
version problem: something you read about in these docs is simply *not there* —
a file the run was supposed to write, a flag the CLI does not accept, a column
that never appears. Check what your deployment is pinned to before concluding
anything is broken: a file the docs describe and a deployment does not write is
usually a pin, not a defect.

A new migration is numbered above the highest one you have applied; apply the
ones you have not, in filename order.

Re-run `quorum resolve-fields` when your Salesforce admin adds a field, installs
or removes a package, or when a column in the artifact stops looking right. The
superseded version is kept, inactive, so what last week's output read stays
answerable.

---

## What this does not support

`docs/supported-configuration.md` is the honest answer to "will this work for
me". Read it before you conclude something is missing — in particular: Gong is
the only meeting source implemented, Salesforce is the only CRM, geography does
not go below country level, and one enrichment provider is implemented —
optional, and off unless configured.
