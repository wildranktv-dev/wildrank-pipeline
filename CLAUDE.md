# Wild Rank pipeline — rules for every production run

Channel: Wild Rank (@WildRank_TV), YouTube ID UCE6icOTx09L4cguDtRUgyTg. English narration; Portuguese subtitles + pt-BR title (auto-dubbing not yet eligible for this channel).
Publishing: Metricool connector, brand/blogId 8409914, timezone America/Sao_Paulo, network "youtube".

## How a video is made
1. Pick the next unchecked topic in `topics.md` (vary formats: ranking, "A vs B", myths vs facts, records).
2. Research every claim with web search. Only use facts you can source; hedge estimates ("estimates vary", "often cited").
   Put the studies or sources in the description. Never invent numbers.
3. Write `jobs/<slug>.json` following `jobs/strongest-animals.json`:
   - 10 ranked segments + intro + outro, about 130–170 words each (target 8–10 min total, ~9–11k characters).
   - Each segment: `queries` + `must` (exact tag names of the animal, e.g. ["tiger","tigers"]), optional
     `fallback` + `fallback_must` for generic footage (shown with an "Illustrative footage" label).
   - `text_pt` = natural Brazilian Portuguese translation of `text` in every segment (used for the PT subtitle file).
   - Top level `title_pt` (max 100 chars) and `description_pt` = Brazilian Portuguese title/description.
   - `stat` = one short on-screen fact. `shorts` = 3 ranks with the most visual/viral appeal. `thumb_rank` = most iconic animal.
4. Commit and push to `main`. GitHub Actions renders and publishes the release `<slug>` with the MP4, 3 Shorts, thumbnail,
   `<slug>_en.srt`, `<slug>_pt.srt` and manifest.
   Public asset URL: https://github.com/wildranktv-dev/wildrank-pipeline/releases/download/<slug>/<file>
5. QA before publishing (mandatory): download the MP4, extract one frame per segment into a grid and look at it.
   Every frame must show the labelled animal (or carry the "Illustrative footage" label). Check the manifest tags too.
   Check the thumbnail and each Short frame. If anything is wrong, fix the job (tighten `must`, change queries) and re-render.
   Never publish a video showing the wrong animal.
6. Schedule with Metricool `createScheduledPost`: long video `type: video`, `privacy: public`, `madeForKids: false`,
   `category: PETS_ANIMALS`, `videoThumbnailUrl` = the thumbnail, first comment with a question. Shorts `type: short`.
   Schedule each Short on a different day at 12:00 or 18:00 local, never two Shorts in the same slot.
7. Subtitles and pt-BR title: automatic. The `localize` workflow (every 30 min) finds the published video by its exact
   title, uploads `<slug>_pt.srt` / `<slug>_en.srt` and sets the pt-BR title/description via the YouTube API. Keep the
   Metricool title identical to the job `title`. After publishing, check the localize run log; report failures.
8. Tick the topic in `topics.md`, add the publish dates, commit, push.

## Content rules (monetization)
- No sexual content or nudity, no children, no reuploaded third-party clips, no graphic violence or animal suffering.
- Original script with analysis in every video. Avoid copy-paste structure between videos.
- Footage only from Pixabay (licence allows commercial use). Narration is an AI voice (disclosed in the description).
