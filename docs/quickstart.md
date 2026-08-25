# Quickstart

From an empty GCP project to an agent people can talk to in Gemini Enterprise.

The README's quick start is the command sequence. This page is the rest of the
way: the project the agents are deployed into, the Terraform root the generated
module calls need, and the one registration a person has to make by hand.

## What you need

- A GCP project with billing enabled
- `gcloud`, Terraform 1.9 or newer, and `uv`
- A Gemini Enterprise app. gete does not create one; make it once in the
  console. Steps 1 to 6 work without it, and step 7 is where it is needed.

If the project is not yours to own, these are the permissions each step wants:

| Who | Roles |
|---|---|
| whoever runs `terraform apply` | `roles/aiplatform.user`, `roles/iam.serviceAccountAdmin`, `roles/resourcemanager.projectIamAdmin`, plus `roles/secretmanager.admin` once an agent declares secrets |
| whoever runs `gete register` | `roles/aiplatform.viewer`, `roles/discoveryengine.admin`, `resourcemanager.projects.get` (e.g. `roles/browser`) unless `gemini_enterprise.project_number` is declared, plus `roles/secretmanager.secretAccessor` on the OAuth secrets of the connections in use |
| the agent's own service account | granted by the module: `roles/aiplatform.user`, and read access to exactly the secrets the declaration names |

## 1. Install gete

```sh
uv tool install "gete[cli] @ git+https://github.com/pepabo/gete"
gete --version
```

The `cli` extra is the command line; the runtime that ships inside the agent
does not need it. Whatever version you install here also decides the module
version the generated Terraform points at, so pin the same one in CI.

## 2. Turn on the APIs and sign in

```sh
PROJECT=my-project

gcloud services enable \
  aiplatform.googleapis.com \
  discoveryengine.googleapis.com \
  iam.googleapis.com \
  cloudresourcemanager.googleapis.com \
  storage.googleapis.com \
  --project="$PROJECT"

gcloud auth application-default login
gcloud auth application-default set-quota-project "$PROJECT"
```

`storage.googleapis.com` is for the Terraform state bucket in step 5. Add
`secretmanager.googleapis.com` as soon as an agent reads a secret — a
`secret_env` entry, a connection, or a shared credential.

## 3. Scaffold the project

```sh
mkdir my-agents && cd my-agents
gete init mail-triage
```

That writes four files:

```
gete.yaml                          project, location, policies
policies/example.yaml              text every agent gets; yours to replace
agents/mail-triage/agent.yaml
agents/mail-triage/instruction.md
```

Put your project id and region in `gete.yaml` (Agent Engine has no `global`
region). Then write the agent's `description` — Gemini Enterprise decides
whether to call the agent from that text, so it is worth more than the display
name — and the `instruction.md` it points at.

`gete validate` accepts the scaffold as it comes, placeholder project id and
all: it checks the shape of the declarations, not whether the names mean
anything yet.

## 4. Try it locally

```sh
export GOOGLE_GENAI_USE_VERTEXAI=TRUE
export GOOGLE_CLOUD_PROJECT="$PROJECT"
export GOOGLE_CLOUD_LOCATION=us-central1

gete validate
gete run mail-triage
```

`gete run` builds the same agent the archive would carry and talks to it with
your own credentials. Where the deployed agent gets a user's token from Gemini
Enterprise, `gete run` takes it from `GETE_TOKEN_<CONNECTION>` and puts it
under the key the runtime reads, so a tool that works here works there.

Once the agent has tools or dependencies of its own, run the deployment-shaped
check before the first apply:

```sh
gete validate --import-check
```

It installs the requirements in a clean environment and imports the
entrypoint. Your working environment has more in it than the agent will get;
this is what catches a missing requirement, which otherwise appears as an
engine that deploys and never starts.

## 5. A Terraform root

`gete terraform` writes one module call per agent and nothing else — no
provider, no backend. Where the state lives is a decision about your
infrastructure rather than about the agent, so that file is yours to write.

```sh
gete terraform --out terraform
```

Add `terraform/main.tf` next to the generated calls:

```hcl
terraform {
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }

  backend "gcs" {
    bucket = "my-tfstate-bucket"
    prefix = "gete"
  }
}

provider "google" {
  project = "my-project"
}
```

**The state holds the agent's source.** The archive is an input-only field on
the engine, so Terraform keeps a copy to know what it deployed: whoever reads
the state reads the code. Keep the bucket as closed as the code deserves.

`gete terraform` leaves files it did not generate alone, so `main.tf` can live
in the same directory. `gete terraform --check` fails when a generated call is
stale, or when one is left behind for an agent that no longer exists — a step
worth having in CI, since a forgotten file keeps an engine alive.

## 6. Deploy

```sh
cd terraform
terraform init
terraform plan
terraform apply
```

`gete` has to be on the PATH wherever Terraform runs: the module packs the
archive by calling `gete archive --external` from a `data "external"`.
Terraform cannot build a byte-identical tar.gz itself, and without that every
plan would show a diff.

The first apply creates the service account, grants it the roles above, and
builds the engine's container; give it several minutes. The engine is labelled
`gete-agent=<name>`, which is how `gete register` finds it afterwards —
display names are never matched on, because the console lets a person choose
them freely.

## 7. List it in Gemini Enterprise

Take the app's id from the console URL, after `engines/`, and declare it:

```yaml
registration:
  gemini_enterprise:
    engine: my-app_1234567890
```

Then:

```sh
gete register
```

`register` creates or updates one authorization per connection and brings the
listing in line with the declaration. Creating the listing in the first place
needs a Gemini Enterprise license; licenses are for people, and one handed to
CD would stay occupied, so gete writes the remaining console steps to
`registration-notice.md` instead. Do them once, and re-run `gete register` (or
let the next release run it) to bind the authorizations.

A listing is never recreated after that. Recreating changes its id, and every
link and session people have to the agent breaks.

## Where to go next

- **Connections** — reading an external service with the user's own
  authorization takes an OAuth client, its id and secret in Secret Manager,
  and `connections: [<id>]` in the declaration. `gete connections` lists what
  ships and what each one reaches.
- **Policies** — `policies/example.yaml` is placeholder text, not a policy.
  What every agent must and must not do belongs there rather than in each
  instruction: written into instructions, the rule is missing wherever
  somebody forgot to paste it.
- **CI** — `gete validate --import-check` and `gete terraform --check` on pull
  requests, `terraform apply` and `gete register` on a tag. Keeping apply
  behind a deliberate release matters: the module decides what a service
  account is allowed to read.
- **Python tools** — only for logic you write yourself. `source: ./src` and a
  `requirements.txt` beside `agent.yaml`; see the README for the declaration
  and [`examples/minimal`](../examples/minimal) for the smallest project that
  has neither.
