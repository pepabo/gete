output "reasoning_engine" {
  description = "Resource name of the reasoning engine."
  value       = google_vertex_ai_reasoning_engine.agent.name
}

output "service_account" {
  description = "Email of the agent's service account."
  value       = google_service_account.agent.email
}

output "source_sha256" {
  description = "sha256 of the archive that was deployed."
  value       = data.external.archive.result.sha256
}
