# Changelog

## [v0.2.14](https://github.com/pepabo/gete/compare/v0.2.13...v0.2.14) - 2026-08-31

- Accept the token Agent Engine forwards as ephemeral state by @haruotsu in https://github.com/pepabo/gete/pull/48

## [v0.2.13](https://github.com/pepabo/gete/compare/v0.2.12...v0.2.13) - 2026-08-31

- Let an openapi block name its own request root by @haruotsu in https://github.com/pepabo/gete/pull/46

## [v0.2.12](https://github.com/pepabo/gete/compare/v0.2.11...v0.2.12) - 2026-08-28

- Name freee's API root in the catalog by @haruotsu in https://github.com/pepabo/gete/pull/43
- Add freee-mcp, the hosted MCP server, to the catalog by @haruotsu in https://github.com/pepabo/gete/pull/44

## [v0.2.11](https://github.com/pepabo/gete/compare/v0.2.10...v0.2.11) - 2026-08-28

- Judge a JWT by its issuer, not its shape by @haruotsu in https://github.com/pepabo/gete/pull/41

## [v0.2.10](https://github.com/pepabo/gete/compare/v0.2.9...v0.2.10) - 2026-08-27

- Let an agent select scopes from a connection's menu by @haruotsu in https://github.com/pepabo/gete/pull/37
- Let a declaration reach into the body and count up what goes out by @haruotsu in https://github.com/pepabo/gete/pull/39
- Prune the description to the declared operations at packing time by @haruotsu in https://github.com/pepabo/gete/pull/40

## [v0.2.9](https://github.com/pepabo/gete/compare/v0.2.8...v0.2.9) - 2026-08-27

- Let a connection be written with PUT and PATCH by @haruotsu in https://github.com/pepabo/gete/pull/33
- Add the Zendesk connection to the catalog by @haruotsu in https://github.com/pepabo/gete/pull/34
- Declare tools from an OpenAPI description by @haruotsu in https://github.com/pepabo/gete/pull/35

## [v0.2.8](https://github.com/pepabo/gete/compare/v0.2.7...v0.2.8) - 2026-08-27

- Say which index an install from git resolves through by @haruotsu in https://github.com/pepabo/gete/pull/25
- Let a connection leave its root open until the installation names it by @haruotsu in https://github.com/pepabo/gete/pull/27
- Offer one reauthorization tool per connection, not per toolset by @haruotsu in https://github.com/pepabo/gete/pull/28
- Let a connection ask for PKCE by @haruotsu in https://github.com/pepabo/gete/pull/29
- Give a connection somewhere to say what a person must do first by @haruotsu in https://github.com/pepabo/gete/pull/30
- Let a connection be read with POST and with fixed headers by @haruotsu in https://github.com/pepabo/gete/pull/31
- Add the Notion MCP endpoint to the catalog by @haruotsu in https://github.com/pepabo/gete/pull/32

## [v0.2.7](https://github.com/pepabo/gete/compare/v0.2.6...v0.2.7) - 2026-08-26

- Document the way from an empty project to a listed agent by @haruotsu in https://github.com/pepabo/gete/pull/22
- Check token elimination per agent, not across the registry by @haruotsu in https://github.com/pepabo/gete/pull/24

## [v0.2.6](https://github.com/pepabo/gete/compare/v0.2.5...v0.2.6) - 2026-08-25

- Let a pattern replace matched digits with their count by @haruotsu in https://github.com/pepabo/gete/pull/19

## [v0.2.5](https://github.com/pepabo/gete/compare/v0.2.4...v0.2.5) - 2026-08-25

- Accept --external without the directory argument by @haruotsu in https://github.com/pepabo/gete/pull/17

## [v0.2.4](https://github.com/pepabo/gete/compare/v0.2.3...v0.2.4) - 2026-08-25

- Resolve packages through the malware-blocking mirror, by declaration by @haruotsu in https://github.com/pepabo/gete/pull/12
- Bump hashicorp/setup-terraform from 3.1.2 to 4.0.1 by @dependabot[bot] in https://github.com/pepabo/gete/pull/8
- Bump actions/checkout from 5.1.0 to 7.0.1 by @dependabot[bot] in https://github.com/pepabo/gete/pull/9
- Bump astral-sh/setup-uv from 7.6.0 to 10.0.1 by @dependabot[bot] in https://github.com/pepabo/gete/pull/10
- Verify ADK 2.7 and widen the pin to include it by @haruotsu in https://github.com/pepabo/gete/pull/15
- Close the gaps in the archive, redaction, and logging boundaries by @haruotsu in https://github.com/pepabo/gete/pull/14
- Post to Slack as a bot behind a declared shared credential by @haruotsu in https://github.com/pepabo/gete/pull/16

## [v0.2.3](https://github.com/pepabo/gete/compare/v0.2.2...v0.2.3) - 2026-08-24

- Pin workflow actions by commit, with Dependabot moving the pins by @haruotsu in https://github.com/pepabo/gete/pull/5
- Ask an owner to review the supply chain surface by @haruotsu in https://github.com/pepabo/gete/pull/6

## [v0.2.2](https://github.com/pepabo/gete/compare/v0.2.1...v0.2.2) - 2026-08-24

- Relock dependencies against pypi.org by @haruotsu in https://github.com/pepabo/gete/pull/3

## [v0.2.1](https://github.com/pepabo/gete/compare/v0.2.0...v0.2.1) - 2026-08-24

- Point at the repository's new home under pepabo by @haruotsu in https://github.com/pepabo/gete/pull/2

## [v0.1.1](https://github.com/haruotsu/gete/compare/v0.1.0...v0.1.1) - 2026-08-24

- Let policies choose the mask texts by @haruotsu in https://github.com/haruotsu/gete/pull/6
- Allow empty env values as declared knobs by @haruotsu in https://github.com/haruotsu/gete/pull/7
- Let connections declare the reauthorization prompt by @haruotsu in https://github.com/haruotsu/gete/pull/10

## [v0.0.1](https://github.com/haruotsu/gete/commits/v0.0.1) - 2026-08-24

- Ship gete inside the archive instead of pinning it from PyPI by @haruotsu in https://github.com/haruotsu/gete/pull/5
