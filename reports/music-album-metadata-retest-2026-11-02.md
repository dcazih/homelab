# Music requester: retest of 37 unresolved albums

2026-11-02. Read-only live MusicBrainz replay; **no music was submitted, downloaded, indexed or made playable**.

- Original 131 attempts: 94 accepted for import, 37 unresolved.
- Improved resolver: **36/37 (97.3%) passed identity and official-release track-count preflight**.
- 1/37 requires manual correction: Cookin Soul — The Experience Album is credited to Diversidad; Cookin Soul was a producer, not the credited album artist.
- Westside Gunn — Flygod and Earl Sweatshirt — Some Rap Songs previously timed out at MCP; check remote request status before any actual retry.
- 94+36=130/131 represents **potential metadata coverage**, not confirmed request or download success.

| Requested | Outcome | Canonical release credit/title | Tracks | Match evidence |
|---|---|---|---:|---|
| MIKE — Pompeii | Metadata OK | Earl Sweatshirt, MIKE & SURF GANG — POMPEII // UTILITY | 33 | artist_credit+canonical_title_extension |
| Gue — FASTLIFE 5: Audio Luxury | Metadata OK | Guè & Cookin’ Soul — Fastlife 5 : Audio Luxury | 15 | artist_credit |
| Tha God Fahim — Supreme Dump Legend: Soul Cook Saga | Metadata OK | Cookin’ Soul & Tha God Fahim — Supreme Dump Legend : Soul Cook Saga | 10 | artist_credit |
| Fiend — Iron Chef | Metadata OK | Cookin’ Soul presents Fiend — Iron Chef | 9 | artist_credit |
| Cookin Soul — DOOM XMAS | Metadata OK | Cookin Soul — DOOM XMAS | 12 | artist_credit |
| Cookin Soul — RAW TAPES Vol. 2 | Metadata OK | Cookin’ Soul — RAW TAPES VOL. 2 | 11 | artist_credit |
| Cookin Soul — The Experience Album | Review | Diversidad — The Experience Album (not verified under Cookin Soul) | — | artist identity conflict |
| Westside Gunn — Heels Have Eyes 3 | Metadata OK | Westside Gunn — HEELS HAVE EYES 3 | 11 | artist_credit |
| Conway the Machine — I Heard You Paint Houses | Metadata OK | Conway — I HEARD YOU PAINT HOUSES | 16 | artist_credit |
| Benny the Butcher — The Plugs I Met 2.5 | Metadata OK | Benny the Butcher — The Plugs I Met 2.5 | 6 | artist_credit |
| Boldy James — Strobelight | Metadata OK | Boldy James & Real Bad Man — Strobelight | 12 | artist_credit |
| Westside Gunn — Flygod | Metadata OK | Westside Gunn — FLYGOD | 18 | artist_credit |
| Westside Gunn — Hitler Wears Hermes 8: Side B | Metadata OK | Westside Gunn — Hitler Wears Hermes 8: Side B | 20 | artist_credit |
| Conway the Machine — Reject 2 | Metadata OK | Conway — Reject 2 | 10 | artist_credit |
| Benny the Butcher — The Plugs I Met | Metadata OK | Benny the Butcher — The Plugs I Met | 7 | artist_credit |
| 2Pac — Thug Life, Volume I | Metadata OK | Thug Life — Thug Life, Volume I | 10 | verified_group_membership |
| 2Pac — The Don Killuminati: The 7 Day Theory | Metadata OK | Makaveli — The Don Killuminati: The 7 Day Theory | 13 | performance_name |
| The Notorious B.I.G. — Born Again | Metadata OK | The Notorious B.I.G. — Born Again | 18 | artist_credit |
| Big L — 139 & Lenox | Metadata OK | Big L — 139 & Lenox | 12 | artist_credit |
| Big L — Return of the Devil's Son | Metadata OK | Big L — Return of the Devil’s Son | 21 | artist_credit |
| Jay-Z — Reasonable Doubt | Metadata OK | Jaÿ‐Z — Reasonable Doubt | 15 | artist_credit |
| Jay-Z — In My Lifetime, Vol. 1 | Metadata OK | Jay‐Z — In My Lifetime, Vol. 1 | 14 | artist_credit |
| Jay-Z — Vol. 2... Hard Knock Life | Metadata OK | Jay‐Z — Vol. 2… Hard Knock Life | 22 | artist_credit |
| Jay-Z — The Blueprint | Metadata OK | Jay‐Z — The Blueprint | 15 | artist_credit |
| Jay-Z — The Black Album | Metadata OK | Jay‐Z — The Black Album | 14 | artist_credit |
| Earl Sweatshirt — Some Rap Songs | Metadata OK | Earl Sweatshirt — Some Rap Songs | 15 | artist_credit |
| Earl Sweatshirt — I Don't Like Shit, I Don't Go Outside | Metadata OK | Earl Sweatshirt — I Don’t Like Shit, I Don’t Go Outside: An Album by Earl Sweatshirt | 10 | artist_credit+canonical_title_extension |
| Freddie Gibbs — Pinata | Metadata OK | Freddie Gibbs & Madlib — Piñata | 17 | artist_credit |
| MF DOOM — Madvillainy | Metadata OK | Madvillain — Madvillainy | 22 | verified_group_membership |
| MF DOOM — Vaudeville Villain | Metadata OK | Viktor Vaughn — Vaudeville Villain | 15 | performance_name |
| MF DOOM — Born Like This | Metadata OK | DOOM — Born Like This | 17 | artist_credit |
| Run the Jewels — Run the Jewels 4 | Metadata OK | Run the Jewels — RTJ4 | 11 | artist_credit+release_title |
| Jay Worthy — LNDN DRGS | Metadata OK | LNDN DRGS & Jay Worthy — LNDN DRGS | 5 | artist_credit |
| Jay Worthy — No Sleep for the Wicked | Metadata OK | Jay Worthy & Sha Hef — No Sleep for the Wicked | 6 | artist_credit |
| Jay Worthy — G-Worthy | Metadata OK | G-Worthy — G-Worthy | 7 | verified_group_membership |
| Jay Worthy — Till the Morning | Metadata OK | Jay Worthy — Till the Morning | 7 | artist_credit |
| J. Cole — Might Delete Later | Metadata OK | J. Cole — Might Delete Later | 12 | artist_credit |

## Method

Read-only script: `scripts/retest_music_album_identity.py`. MusicBrainz release-group search, exact normalized titles and cautious canonical suffixes, artist and member-group relationship MBIDs, aliases and performance-name relationships; official release statuses and track counts. No DroppedNeedle POSTs, no NAS access.
