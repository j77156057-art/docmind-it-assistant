# Security Policy

## Supported Versions

Security fixes are applied to the latest commit on `main`.

## Reporting a Vulnerability

Please use GitHub private vulnerability reporting when it is available for this repository. Do not include credentials, access tokens, private documents, or production data in a public issue.

Include the affected component, reproduction conditions, expected impact, and a minimal proof of concept. Reports will be acknowledged as soon as practical; no response-time SLA is promised for this demonstration project.

## Deployment Boundary

The default configuration is for local demonstration only. Production deployments must use OIDC, PostgreSQL, provider-backed embeddings, a strong subject salt, and separate network policies for the query and administration services.
