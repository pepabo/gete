### {title}

{lead}

The authorizations were created or updated by this release. **Only the registration remains.**

Creating an agent in Gemini Enterprise needs a Gemini Enterprise license. Licenses are for people; one handed to CD would stay occupied, so CD does not hold one.

Open the [agent list]({console_url}) → "Add agent" → "Custom agent on Agent Runtime", and enter:

| Field | Value |
|---|---|
| Name | `{display_name}` |
| ID (via "Edit" under the name) | `{name}` |
| Description | {description} |
| Agent Runtime reasoning engine | `{reasoning_engine}` |

**Type the name in ASCII.** The console derives the ID from the name; with other scripts the ID comes out empty and "Create" stays disabled. Rename it afterwards if you like: CD matches on the reasoning engine, not the display name.

At the Authorizations step press **Skip.** The authorizations already exist; creating them again under the same name collides.

That is all. The authorizations are bound on the next release (or the next run of `gete register`).

<details><summary>Bind them by hand instead</summary>

```bash
AGENT=<resource name of the agent you created>
curl -sS -X PATCH -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" -H "X-Goog-User-Project: {project}" \
  "https://discoveryengine.googleapis.com/v1alpha/${{AGENT}}?updateMask=authorizationConfig" \
  -d '{authorization_config_json}'
```

</details>
