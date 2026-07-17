# modal-devin

Run [Devin Outposts](https://docs.devin.ai/cloud/outposts/overview) sessions on
Modal.

Devin's planning remains in Devin Cloud while commands, file edits, and repository
access run in isolated [Modal Sandboxes](https://modal.com/docs/guide/sandboxes) that
you control. [Modal provides the
orchestrator](https://docs.devin.ai/cloud/outposts/overview#how-it-works) through
`modal-devin`: it watches the Devin queue, starts a Sandbox for each session,
preserves suspended work, and cleans up when the session ends.

> [!NOTE]
> `modal-devin` is alpha software. Until the first stable release, only the latest
> release on the default branch receives security fixes.

## When to use it

Use `modal-devin` when you want Devin sessions to:

- Run with custom tools, repositories, and system packages.
- Reach services and registries available from your Modal environment.
- Use Modal compute, memory, and region controls.
- Resume from a preserved workspace after suspension.
- Fit into an existing Python and Modal deployment workflow.

## How it fits into Outposts

Following [Devin's Outposts model](https://docs.devin.ai/cloud/outposts/overview#how-it-works),
there are two runtime layers:

- The **worker** serves one queued Devin session and executes its tool calls.
  `modal-devin` runs a worker inside an isolated Modal Sandbox.
- The **orchestrator** watches for queued sessions, provisions compute, and starts a
  worker for each one. `modal-devin` deploys the orchestrator as a Modal application.

Workers need outbound HTTPS access to Devin. They do not require inbound ports or a
public IP.

An **outpost** is a named queue of sessions served by your infrastructure. It is not
a machine that you set up locally. After deployment, the orchestrator runs on Modal,
and each session runs in its own Modal Sandbox; your local machine is only used to
configure and deploy the application.

## Quick start

Before starting, you need:

- Python 3.11 or newer and a [Modal account](https://modal.com/docs/guide/modal-user-account-setup).
- A Devin organization with Outposts enabled. Outposts is currently early access;
  contact your Cognition account team if it is not available.
- A Devin service user token with the Outposts permissions described below.

### 1. Create the Devin service user token

In the Devin web app:

1. Under **Settings → Roles**, create an enterprise role with **Use outpost
   machine** (`UseOutpostsMachine`) and **Manage outposts**
   (`ManageOutpostsOrchestrator`) enabled.
2. Under **Settings → Devin API → Service users**, provision a service user with
   that role.
3. Copy the `cog_...` token when it is shown. Devin only displays it once.

These permissions give the token the `account.outposts.machine` and
`account.outposts.orchestrator` scopes. See Devin's [Outposts
prerequisites](https://docs.devin.ai/cloud/outposts/overview#prerequisites) and
[permissions reference](https://docs.devin.ai/api-reference/v3/overview) for more
detail.

### 2. Create and deploy the outpost

```bash
uv add modal-devin
uv run modal setup
uv run modal-devin init my-outpost --no-deploy
uv run modal-devin doctor
uv run modal-devin deploy outposts/my_outpost.py
```

`init` prompts for the Devin token, creates the named outpost through Devin's
Outposts API, offers to store the token as a Modal Secret, and generates the Modal
application. You do not need to install the Devin CLI or run a worker on your local
machine. Run `uv run modal-devin init` without arguments for the fully interactive
wizard.

If you already created an outpost in the Devin web app or with
[`devin worker outpost create`](https://docs.devin.ai/cloud/outposts/overview#2-create-an-outpost),
connect it instead by passing its `outpost_env-...` ID:

```bash
uv run modal-devin init my-outpost --outpost-id outpost_env-... --no-deploy
```

### 3. Start a Devin session

In Devin Cloud, start a session and select `my-outpost` as its machine. Devin puts
the session in the outpost's queue; the Modal orchestrator claims it and starts its
worker in a new Sandbox.

The generated Python file is your Modal application. Edit it to choose the workspace
image, secrets, compute, region, retry policy, and schedule. The library manages the
Outposts and Sandbox lifecycle behind that application.

## Customize the workspace

Add packages and project source to the generated image block, before
`prepare_image()`:

```python
base_image = (
    worker.base_image()
    .apt_install("ripgrep")
    .run_commands(
        "git clone https://github.com/your-org/app /root/workspace/app"
    )
)
image = worker.prepare_image(base_image)
```

## Documentation

Full tutorials, how-to guides, and reference docs live in this repository under
`tutorials/`, `guides/`, `explanation/`, and `reference/`. They're written for the
project's Mintlify site, which isn't published yet, so some page components won't
render when viewed directly on GitHub:

- [Deploy your first worker](tutorials/first-worker.mdx)
- [Build a project workspace](tutorials/custom-workspace.mdx)
- [Configure resources and regions](guides/resources-and-regions.mdx)
- [Operate a deployed worker](guides/operate-a-worker.mdx)
- [Security model](explanation/security.mdx)

## Security

Use a least-privilege Devin service user and separate credentials for environments
with different trust requirements. The session workload does not receive the raw
Outposts token in its environment, although it can exercise the Outposts capability
required to serve its assigned session.

Report vulnerabilities through
[GitHub private vulnerability reporting](https://github.com/aaazzam/modal-devin/security).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development and test workflow.
