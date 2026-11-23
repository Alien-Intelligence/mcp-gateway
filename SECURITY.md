# Security policy

## Reporting a vulnerability

Do not open a public issue for a security problem. Write to **security@alien.club** with:

- what you found and which component is affected (OAuth validation, resolver, proxy, observability, container image, Helm chart);
- the gateway version (the image tag or the package version) and the configuration that matters;
- steps or a proof of concept that reproduce it.

You will get an acknowledgement within 3 working days and a first assessment within 10 working days. We will tell you when a fix is released and credit you in the release notes unless you prefer otherwise.

Please give us a reasonable time to ship a fix before you disclose. Do not access data that is not yours, and do not run tests against a deployment you do not operate.

## Supported versions

The project is pre-1.0. Only the latest released minor version receives security fixes. Upgrade to it before reporting, unless the problem is the upgrade itself.

## Scope

In scope:

- bypassing JWT validation (signature, `iss`, `aud`, `exp`, `nbf`, algorithm handling);
- sending a request upstream with another user's API key, or without a resolved identity;
- credentials or forgeable headers reaching the upstream (`Authorization`, `Cookie`, `X-Forwarded-*`, and the other stripped headers);
- API keys, tokens, user identifiers or tool arguments leaking into logs, traces, metrics or error responses;
- SSRF or request smuggling through the proxy, the webhook resolver or the JWKS fetch;
- weaknesses in the shipped container image and Helm chart defaults.

Out of scope:

- vulnerabilities in your OAuth server, your credential store or your webhook implementation;
- findings that need a misconfiguration the documentation warns against (for example disabling TLS verification);
- denial of service by raw volume from an already authenticated user, beyond the documented limits;
- vulnerabilities in third-party dependencies that are not reachable from the gateway (report those upstream).
