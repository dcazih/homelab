# Family Music Connector

## Purpose

Family Music is an optional music-request integration for Muse. Use it when an authorized family member asks you to find, add, or request music or to check on a previous request. Continue to perform all other tasks normally.

The household's existing music services are:

- **Navidrome:** shared music catalog, separate user accounts, and each person's `My Music` playlist.
- **Cassette:** personal music listening interface backed by Navidrome.
- **DroppedNeedle:** manages missing-music acquisition and any required approvals or quotas.
- **slskd:** part of the underlying acquisition infrastructure.
- **Music Profiles Bridge:** identifies the requesting user, searches Navidrome, adds existing recordings to personal collections, queues missing-music requests, and reconciles completed requests.
- **Family Music connector:** a restricted MCP interface that authenticates family members and exposes music operations from the Bridge.

Do not bypass the connector to administer the underlying services. The connector does not store audio or provide the music-player interface.

## Connection configuration

```yaml
name: Family Music
protocol: MCP
transport: Streamable HTTP
server_url: https://homelab.hippocampus-polaris.ts.net/mcp
authentication: OAuth 2.0 Authorization Code with PKCE (S256)
scopes:
  - music:read
  - music:write
```

**Connection status:** The URL above is the configured *intended* endpoint. It has not been verified here as externally reachable or registered successfully with Muse. Check actual connectivity before claiming this connector is operational. If its host is unavailable, report the connection failure; do not silently substitute an address.

### Establishing the connection

1. Check whether the `Family Music` connector is already available and authorized. If it is, use it; do not create a duplicate.
2. If it is not connected and Muse supports adding custom remote MCP connectors, register it using the `server_url` above. Use the normal OAuth discovery and supported client-registration flow. Do not invent client IDs, client secrets, access tokens, or manually configured OAuth endpoints.
3. Start the OAuth authorization flow for the family member making the request. The connector's authorization page should identify **Family Music** and ask for that person's **Bridge profile token**. The person enters the token on the connector's secure HTTPS authorization page, not in chat. Never request the Bridge administrator key.
4. When authorization completes, discover the MCP tools and their current input schemas. Check that the required scopes are granted. Each family member must separately authorize their own profile.
5. Run a harmless `music_search` as a connectivity and authorization test. Do not mark the integration ready until a real authenticated tool response succeeds.
6. If Muse cannot register custom MCP connectors or cannot complete the OAuth flow, report the exact missing capability or error. Do not claim a connection was created.

Only the public MCP server URL is needed for Muse to connect. Internal server addresses, Docker networking, local ports, installation commands, and secret configuration values are not needed for normal Muse operation.

## Available music tools

The intended connector exposes these tools. Prefer the names and schemas returned by actual MCP discovery if the live server differs.

| Tool | Purpose | Expected arguments |
| --- | --- | --- |
| `music_search` | Search Navidrome for recordings | `artist`, `title` |
| `music_add_existing` | Add an existing recording to the signed-in user's `My Music` | `song_id` |
| `music_request_song` | Submit a specific song through the Bridge request/approval process | `artist`, `title`; optional `album`, `recording_mbid` |
| `music_request_album` | Submit an album request if the live Bridge supports the required schema | `artist`, `album`; optional `release_mbid` |
| `music_list_requests` | List the signed-in person's current and past requests | No arguments |
| `music_get_request` | Retrieve the status of a specific request | `request_id` |

Treat tool calls as user-scoped. The connected identity determines whose library and request history are affected. Never use another family member's authorization or infer access from an administrator account.

## Request handling

### Search or add a song

1. Interpret the artist, title, and any specified recording or version.
2. Use `music_search` before requesting acquisition.
3. If exactly one suitable match exists in Navidrome, call `music_add_existing` with its confirmed `song_id` when the user asked to add it.
4. If the same song ID is already in the user's `My Music`, report that it is already present. Do not add another identical entry or download another physical copy.
5. If several plausible versions exist, ask which one the user wants. Do not guess between live recordings, remasters, clean/explicit versions, or different artists with the same song title.
6. If no suitable recording exists and the user asked to obtain it, use `music_request_song` with the available identifying metadata.
7. Follow the request status returned by the Bridge. A request can be awaiting approval, need additional metadata, be queued for acquisition, fail, or complete later.

### Request an album

Use `music_request_album` only if the live connector exposes it and its request succeeds against the Bridge's actual schema. If album requests are unsupported or rejected, explain the specific limitation rather than silently switching to an incomplete single-song request.

### Check progress

Use `music_list_requests` or `music_get_request` for the authenticated family member. Distinguish between a request being created, approved, queued, acquired, indexed by Navidrome, and added to `My Music`. Never describe a pending request as downloaded or playable.

### Responding to the user

Keep the response concise and action-specific. State precisely whether the music was:

- Already in the shared Navidrome catalog.
- Added to that person's `My Music`.
- Already present in their personal collection.
- Submitted as a request and awaiting acquisition or approval.
- Held for choosing a specific version or supplying metadata.
- Blocked by a permission, quota, service error, or unavailable integration.

If the connector is not linked, offer to start its OAuth connection flow before attempting a write. Do not claim success based on intentions, local tests, or documentation alone.

## Examples

**“Add SZA's Kill Bill to my music.”** Search Navidrome. If an exact version is found, add that recording to the current user's `My Music`; otherwise submit the song request if authorized. Report the actual result.

**“Request the album DAMN. by Kendrick Lamar.”** Use the album request tool when supported. Report whether it was accepted or if the live backend requires additional information.

**“Is this song already on our server?”** Search only. Do not add or request it without authorization.

**“What's happening with the music I requested?”** List only the signed-in user's requests and report their actual status.

## Authorization, identity, and privacy

- Use OAuth 2.0 authorization with PKCE; obtain only the necessary `music:read` and `music:write` grants.
- Each family member connects their own existing Music Profiles Bridge profile.
- The **Bridge profile token** must be supplied only to the connector's HTTPS authorization page, never pasted into Muse conversations or memory files.
- Never use, request, display, or store the Bridge administrator key in Muse's memory.
- Never expose OAuth access tokens, refresh tokens, API credentials, or passwords in responses.
- Never make changes to another person's library without that person's authorization.
- Respect Bridge duplicate protections, DroppedNeedle approvals, quotas, and rate limits.
- If a personal Bridge token was replaced, previous authorizations may no longer work. Request a new OAuth authorization rather than trying to recover the original token.

## Scope and boundaries

For the current Stage 2, focus only on **music search, adding existing music, new music requests, and request status**. Spotify, Apple Music, YouTube Music, listening-history exploration, and external-library transfers are postponed. Do not start those integrations as part of a normal music request.

The connector is an additional capability; use it when music requests are relevant and otherwise continue normal Muse operations.

## Operational verification

Consider the connector **working** only when all of the following are true:

1. The public MCP URL is reachable from Muse.
2. MCP and OAuth discovery are supported.
3. The family member has completed the connector authorization flow.
4. The six advertised tools, or a verified compatible subset, are discoverable.
5. An authenticated read tool call succeeds.
6. Write actions are reported as successful only after confirmed tool responses.

If any step fails, state which step is blocked. The connector has been locally tested, but this does not establish that cloud Muse is connected to the user's live Mac mini or that DroppedNeedle has fulfilled a particular request.
