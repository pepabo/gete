variable "project_id" {
  type        = string
  description = "GCP project that holds the reasoning engine, its service account, and the secrets it reads."
}

variable "location" {
  type        = string
  description = "Region of the reasoning engine. Agent Engine has no global region."
}

variable "name" {
  type        = string
  description = "The agent's name from agent.yaml. Names the service account and labels the engine."

  # The service account id is "<name>-ae", and GCP wants 6 to 30 characters.
  # Without this the module plans and fails halfway through an apply, with a
  # service account created and the engine missing.
  validation {
    condition     = length("${var.name}-ae") >= 6 && length("${var.name}-ae") <= 30
    error_message = "name must be 3 to 27 characters: the service account is \"${var.name}-ae\"."
  }
}

variable "display_name" {
  type        = string
  description = "Name shown in Gemini Enterprise."
}

variable "description" {
  type        = string
  description = "What the agent does. Gemini Enterprise uses it to decide when to call the agent."
}

variable "agent_directory" {
  type        = string
  description = "Directory holding agent.yaml. gete packs it; the module never reads the files itself."
}

variable "env" {
  type        = map(string)
  description = "Environment variables for the agent. Empty values are not sent."
  default     = {}
}

variable "secret_env" {
  type        = map(string)
  description = "Environment variable name to Secret Manager secret name. Values never enter Terraform."
  default     = {}
}

variable "python_version" {
  type        = string
  description = "Python the engine runs. Keep it equal to the version gete was developed against."
  default     = "3.12"
}

variable "min_instances" {
  type    = number
  default = 0
}

variable "max_instances" {
  type    = number
  default = 1
}

variable "labels" {
  type        = map(string)
  description = "Extra labels. gete-agent and gete-source are set by the module."
  default     = {}
}
