# One agent on Agent Engine: the archive gete packs, a service account that
# can call Vertex AI and read the declared secrets, and the reasoning engine.

# Terraform cannot build a tar.gz itself, and the archive has to come out
# byte-identical for unchanged input or every plan would show a diff. gete
# owns that; the module only asks for the result.
data "external" "archive" {
  program = ["gete", "archive", "--external"]
  query = {
    directory = var.agent_directory
  }
}

resource "google_service_account" "agent" {
  project      = var.project_id
  account_id   = "${var.name}-ae"
  display_name = "${var.name} (Agent Engine)"
}

# Enough to call the models. External services are read with tokens the user
# authorized in Gemini Enterprise, so the agent holds no credentials of its own.
resource "google_project_iam_member" "vertex_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.agent.email}"
}

# Exactly the secrets the declaration names. Granted by hand, a secret added
# to the declaration would start the agent without access and fail on first use.
resource "google_secret_manager_secret_iam_member" "secret_reader" {
  for_each = toset(values(var.secret_env))

  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent.email}"
}

resource "google_vertex_ai_reasoning_engine" "agent" {
  project      = var.project_id
  region       = var.location
  display_name = var.display_name
  description  = var.description

  # gete register finds the engine by gete-agent; gete-source is the start of
  # the archive's sha256, the only way to tell what was deployed, since the
  # archive itself is input-only.
  labels = merge(var.labels, {
    gete-agent  = var.name
    gete-source = substr(data.external.archive.result.sha256, 0, 16)
  })

  # An engine that was ever called owns sessions, and without FORCE its
  # deletion fails with "delete child resources first". Not ABANDON: an engine
  # dropped from state but left in GCP belongs to nobody.
  deletion_policy = "FORCE"

  spec {
    agent_framework = "google-adk"
    service_account = google_service_account.agent.email

    source_code_spec {
      inline_source {
        # sensitive() keeps tens of kilobytes of base64 out of the plan. It
        # does not keep the archive out of the state, where this attribute
        # and the data source's result both persist: whoever reads the state
        # reads the agent's source. That is why packing refuses paths outside
        # the agent's directory and leaves hidden files (.env, .git) behind.
        source_archive = sensitive(data.external.archive.result.archive)
      }

      python_spec {
        # gete writes gete_entry.py at the archive root; it exposes app, the
        # AdkApp. Pointing at the agent itself would not start.
        entrypoint_module = "gete_entry"
        entrypoint_object = "app"
        requirements_file = "requirements.txt"
        version           = var.python_version
      }
    }

    deployment_spec {
      min_instances = var.min_instances
      max_instances = var.max_instances

      # Empty values are dropped: Agent Engine rejects them outright.
      dynamic "env" {
        for_each = { for key, value in var.env : key => value if value != "" }
        content {
          name  = env.key
          value = env.value
        }
      }

      # Only the secret's name reaches Terraform. The value would otherwise
      # sit in the state for everyone who can read it.
      dynamic "secret_env" {
        for_each = var.secret_env
        content {
          name = secret_env.key
          secret_ref {
            secret  = secret_env.value
            version = "latest"
          }
        }
      }
    }
  }
}
