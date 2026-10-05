# Cutover leftovers — what needs a decision (2026-10-05)

Source: the production copy of 10-04 (after removing the duplicate levels), `lcars rulecheck`, and the rebuild reports. Nothing was
changed. "Decision" below means a choice only you can make; everything else is waiting on the drip or on me.

## A. Levels that hold an AniList entry but no episodes and no place in the numbering — 61 (rulecheck R1.11i)

These are `special` levels the rebuild made for entries on your AniList/MAL lists that TVDB/Sonarr don't have episodes for. They are
exempt from the "every level has a span" rule on purpose (list-only entries), so **nothing is wrong by default**. The only question
is whether any of them *belong inside a series' numbering*, which would give them a number and a span (R1.4: a film that is an
integral part of the story takes a whole number between seasons).

### A1. Nine entries whose AniList format is TV (or TV short) — please look first
A TV-format entry held as a special with no episodes is unusual: it may really be a season or a part of that show (Aldnoah.Zero
Season 2 is one). **Decision per row:** is it a season/part of the show (then it gets episodes/a span through the normal route, and
I look at why it was not linked), or a standalone entry that is fine as it is?

| # | Show | Film / special (AniList) | Year | Format | Status in LCARS |
|---|---|---|---|---|---|
| 4 | Aldnoah.Zero | ALDNOAH.ZERO Season 2 (AniList 20853) | 2015 | TV | completed |
| 16 | Durarara!! | Durarara!! X2 (AniList 20652) | 2015 | TV | planned |
| 25 | Hoozuki no Reitetsu | Hozuki’s Coolheadedness 2 (AniList 98438) | 2017 | TV | planned |
| 31 | Kankin Kuiki Level X | Lockdown Zone: Level X Season 2 (AniList 182877) | 2025 | TV_SHORT | completed |
| 44 | R.O.D -READ OR DIE- | R.O.D -THE TV- (AniList 209) | 2003 | TV | planned |
| 45 | Rurouni Kenshin: Meiji Kenkaku Rom | Rurouni Kenshin: Meiji Kenkaku Romantan 3rd Season (AniList 188665) |  | TV | planned |
| 47 | Sekai Saikou no Ansatsusha, Isekai | Sekai Saikou no Ansatsusha, Isekai Kizoku ni Tensei suru 2nd (AniList 169579) | 2027 | TV | planned |
| 48 | Shingeki no Kyojin | Attack on Titan Season 3 Part 2 (AniList 104578) | 2019 | TV | planned |
| 49 | Skip to Loafer | Skip and Loafer Season 2 (AniList 185657) | 2027 | TV | planned |

### A2. Four films
**Decision per row:** integral to the story (it then takes a whole number between seasons and a span, R1.4) or standalone (stays as is)?

| # | Show | Film / special (AniList) | Year | Format | Status in LCARS |
|---|---|---|---|---|---|
| 2 | Adieu Galaxy Express 999 | Adieu Galaxy Express 999 (AniList 1302) | 1981 | MOVIE | completed |
| 5 | Aldnoah.Zero | Aldnoah.Zero (Re+) (AniList 185714) | 2025 | MOVIE | completed |
| 58 | Uchuu Kaizoku Captain Harlock | Harlock: Space Pirate (AniList 17269) | 2013 | MOVIE | completed |
| 59 | Uchuu Kaizoku Captain Harlock | Space Pirate Captain Harlock: Riddle of the Arcadia Episode (AniList 2470) | 1978 | MOVIE | completed |

### A3. The other 48 (OVAs, ONAs, specials, music videos)
List-only entries: **no decision needed** unless you want one of them placed in its show's numbering. They are listed so you can
say "that one is part of the story" if any is.

| # | Show | Film / special (AniList) | Year | Format | Status in LCARS |
|---|---|---|---|---|---|
| 1 |  | Rescue ME! (AniList 18039) | 2013 | OVA | completed |
| 3 | Aki-Sora | Aki Sora (AniList 8577) | 2010 | OVA | completed |
| 6 | Amagi Brilliant Park | Amagi Brilliant Park: No Time to Take It Easy! (AniList 21077) | 2015 | OVA | completed |
| 7 | Arifureta Shokugyou de Sekai Saiky | Arifureta Shokugyou de Sekai Saikyou Prologue (AniList 145560) | 2020 | ONA | completed |
| 8 | Arifureta Shokugyou de Sekai Saiky | Arifureta: From Commonplace to World's Strongest Specials (AniList 111729) | 2019 | SPECIAL | completed |
| 9 | Arifureta Shokugyou de Sekai Saiky | Arifureta - From Commonplace to World's Strongest: The Mirac (AniList 146921) | 2022 | OVA | planned |
| 10 | Bakemonogatari | Uji ni wa Monogatari ga Aru (AniList 175783) | 2024 | SPECIAL | completed |
| 11 | Barakamon | Barakamon: Mijikamon (AniList 20782) | 2014 | ONA | completed |
| 12 | Boku no Hero Academia | Boku no Hero Academia: Sukue! Kyuujo Kunren! (AniList 87486) | 2017 | OVA | completed |
| 13 | Boku no Kokoro no Yabai Yatsu | Setting Sun (AniList 165066) | 2023 | MUSIC | completed |
| 14 | Dorohedoro | Dorohedoro Season 3 (AniList 212618) |  |  | planned |
| 15 | Dungeon Meshi | Delicious in Dungeon CM (AniList 111516) | 2019 | ONA | completed |
| 17 | Edomae Elf | Kien Romance (AniList 164193) | 2023 | MUSIC | completed |
| 18 | Elf-san wa Yaserarenai. | Plus-Sized Elf Specials (AniList 179126) | 2024 | ONA | completed |
| 19 | Ginga Tokkyuu Milky☆Subway | Milky☆Byway Spring Special: Terror on Chelovin's Day (AniList 213867) |  | SPECIAL | planned |
| 20 | Golden Kamuy | Golden Kamuy OVA (AniList 101830) | 2018 | OVA | completed |
| 21 | Golden Kamuy | Golden Kamuy: Inazuma Goutou to Mamushi no Ogin/Shimaenaga (AniList 194872) | 2025 | OVA | planned |
| 22 | Golden Kamuy | Golden Kamuy: Saishuushou - Bousou Ressha-hen (AniList 210073) |  |  | planned |
| 23 | Henjin no Salad Bowl | Gifted (AniList 177406) | 2024 | MUSIC | completed |
| 24 | Hokuto no Ken | Voices of a Distant Star (AniList 256) | 2002 | OVA | completed |
| 26 | Ichigo 100% | Strawberry 100% (AniList 641) | 2005 | SPECIAL | completed |
| 27 | Ichigo 100% | Strawberry 100% Special (AniList 642) | 2004 | SPECIAL | completed |
| 28 | K-ON! | K-ON!: Live House! (AniList 6862) | 2010 | OVA | completed |
| 29 | K-ON! | K-ON! Season 1 Shorts (AniList 7017) | 2009 | SPECIAL | completed |
| 30 | Kaijuu 8-gou | Kaiju No.8: Hoshina's Day Off (AniList 179999) | 2025 | SPECIAL | completed |
| 32 | Kidou Keisatsu Patlabor ON TELEVIS | Mobile Police Patlabor: The New Files (AniList 1289) | 1990 | OVA | paused |
| 33 | Kobayashi-san Chi no Maidragon | Miss Kobayashi's Dragon Maid: Valentines and Hot Springs! (P (AniList 98580) | 2017 | OVA | completed |
| 34 | Kobayashi-san Chi no Maidragon | Miss Kobayashi’s Dragon Maid S: Japanese Hospitality (The At (AniList 136436) | 2022 | OVA | completed |
| 35 | Kokoro Connect | Kokoro Connect ~ The OVAs (AniList 16001) | 2012 | OVA | completed |
| 36 | Kono Subarashii Sekai ni Shukufuku | KONOSUBA -God's Blessing on This Wonderful World! 3 -BONUS S (AniList 181244) | 2025 | OVA | planned |
| 37 | Kyokou Suiri | Kyokou Uranai (AniList 122238) | 2020 | ONA | dropped |
| 38 | Love Hina | Love Hina Christmas Movie (AniList 191) | 2000 | SPECIAL | completed |
| 39 | Love Hina | Love Hina Spring Movie (AniList 192) | 2001 | SPECIAL | completed |
| 40 | Love Hina | Love Hina: Motoko's Choice, Love or the Sword: Don't Cry (AniList 963) | 2000 | SPECIAL | completed |
| 41 | Moyashimon | Moyashimon CGI Anime (AniList 9290) | 2010 | OVA | completed |
| 42 | Odekake Kozame | Odekake Kozame × Chiba-ken Kuro Ajillo (AniList 173534) | 2024 | ONA | completed |
| 43 | Onee-chan ga Kita | Onee-chan ga Kita: Hajimete no… Kitaa! (AniList 20682) | 2014 | OVA | completed |
| 46 | Samurai I: Musashi Miyamoto | Muramata-san no Himitsu (AniList 118656) | 2020 | OVA | completed |
| 50 | Sousou no Frieren | Sousou no Frieren: ●● no Mahou Part 2 (AniList 189513) | 2025 | ONA | completed |
| 51 | Sword Art Online | Sword Art Offline: Extra Edition (AniList 20895) | 2014 | SPECIAL | completed |
| 52 | The Rookie | Urotsukidoji: Legend of the Overfiend (AniList 2341) | 1987 | OVA | completed |
| 53 | The Rookie | THE UROTSUKI (AniList 1353) | 2002 | OVA | completed |
| 54 | Toaru Kagaku no Railgun | Toaru Kagaku no Railgun: Entenka no Satsuei Model mo Raku ja (AniList 9063) | 2010 | OVA | completed |
| 55 | Toradora! | Toradora!: Bento Battle (AniList 11553) | 2011 | OVA | completed |
| 56 | Toradora! | Toradora!: SOS! Hurray for Foodies (AniList 6127) | 2009 | SPECIAL | completed |
| 57 | Uchuu Densetsu Ulysses 31 | Ulysses 31 Pilot (AniList 10701) | 1980 | SPECIAL | completed |
| 60 | Yasei no Last Boss ga Arawareta! | A Wild Last Boss Appeared! Season 2 (AniList 204389) | 2026 | ONA | planned |
| 61 | Yuru Camp△ | ROOM CAMP: Saunas and Grub and Three-Wheeler Bikes (AniList 114981) | 2020 | OVA | completed |

## B. Urusei Yatsura
Nothing left to decide beyond what you already did: the four TVDB seasons all carry AniList 1293 (one entry spanning them — the
list-sync rule you gave on 10-05, progress by episode and status mirroring the last AniList status, covers it; not built yet); the
17 films are skip-listed (skipped = off the lists, kept). The five "films/specials with no level" now have special levels
(skipped or planned, no episodes).

## C. Battle Angel (1993)
Three shows touch it: **Battle Angel** (movie, Radarr 17189, AniDB 147), **GUNNM** (episodic, TVDB 79295, AniDB 147, completed)
and **Alita: Battle Angel** (the 2019 film, a different work). AniDB 147 sits on two shows. **Decision:** are "Battle Angel"
(the 1993 OVA as a movie) and the GUNNM series entry the same thing to track once (merge / keep one), or two things? A movie has
no TVDB series id by rule (R3.2a), so that gap is expected and needs nothing.

## D. Suzumiya Haruhi S1 — no decision
The 2006 (AniList 849) and 2009 (4382) cours are part levels with no episodes and no spans; TVDB S1 holds all 14 episodes. They get
their spans when AniDB data for those two arrives (the drip). If they are still empty after the drip I will come back with a proposal.

## E. Not yet looked at (no decision yet)
51 AniList history entries that don't map to a level · 78 levels not linked to a list entry · 67 levels held back (the list counts a
different number of episodes — status written, progress not) · Mushoku Tensei 146065 episode 0. They come from the rebuild reports
in `~/starfleet-rebuild/run2/`; I will go through them after the drip and bring only the ones that need you.
