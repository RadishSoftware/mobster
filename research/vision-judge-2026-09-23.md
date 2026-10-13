# VisionJudge: calibrated visual predicates on element-anchored crops (2026-09-23)

This memo implements and measures the perception-router proposal (P3) of an earlier visual-loops research memo (not published).
VisionJudge answers narrow questions about image content ("is there a dog", "is this a receipt", "what colour
are this person's eyes") on crops the caller cuts at an AX element's frame. It returns a yes/no/enum answer or
abstains with a reason. It never picks action targets. The phone was not used. All numbers come from a
labelled offline set run live against the configured Vertex helper (`gemini-3.5-flash-lite`) on 23 Sep 2026.

## Summary

* **Final cascade on 135 labelled crops:** Apple Vision T0, then batched Flash-Lite T1, then a
  higher-resolution look again (T2), then abstain.
  * 1 error in 117 answers (99.1% precision when answering).
  * dog 30/30, sunglasses 28/28, receipt 39/40.
  * Eye colour: 19/19 correct when answering, coverage 79% of answerable crops, and all 13 unanswerable
    crops (black and white, sunglasses, profile, animal) correctly abstained.
  * These are small samples. The one-sided 95% Clopper–Pearson lower bound on eye-colour precision is only
    **85%** (0 errors in 19). Use the numbers as a design check, not a certification.
* **Decomposition is what makes eye colour safe.** One holistic question gave 5 wrong answers out of 27
  (81.5% precision) and named an eye colour for 3 of the 13 unanswerable images (for example "hazel" for a
  black-and-white photo). Gated sub-questions (face, then eyes visible, then colour photo, then iris), asked in
  the same request, produced 0 false answers on unanswerable images.
* **Self-reported confidence cannot catch semantic mistakes, but a gate can.** A product photo of sunglasses
  was judged "worn" at confidence 1.0. A person gate (`person_gate_steps`) fixed it. A deli price label
  ("Total price $8.00") was called a receipt at 0.99–1.0 by both Flash-Lite models, and logprobs gave it
  p = 1.0 as well. That is the one remaining error.
* **Latency (T1 on `gemini-3.5-flash-lite`, low media resolution):**
  * one crop: 0.89 s p50 / 1.11 s p90
  * eight crops in one request: 1.62 s p50 / 2.08 s p90, about 0.2 s per crop
  * decomposed eye colour, eight crops: about 2.1 s
  * T2 on one crop at high resolution: 1.2–1.6 s
  * T0: **8.5 ms** per crop wall-clock (6.5 ms of Vision compute)
* **Tail risk found and fixed.** 3 of 24 single-crop requests stalled until the 20 s deadline. The other
  21 finished in 0.75–1.25 s. The judge now hedges: after 1.2 s + 0.2 s per crop it sends an identical twin
  request and takes the first answer. A failed request gets one retry after 0.25 s. There were 13 HTTP 429
  responses during the eval, 12 of them on 2.5-flash-lite.
* **T0 decided 20 of the 98 binary crops locally with 0 errors:** 9/30 dog, 6/40 receipt, 5/28 sunglasses.
  It confirms positives only. Apple Vision missed small dogs completely (score 0.00), so it must never
  decide "no".
* **Warm connections.** The first-helper-call penalty in `latency-breakdown` (+455 ms) is mostly the
  `gcloud auth print-access-token` subprocess (**474–738 ms** when the 5-minute token cache is cold), not
  TLS. A TCP+TLS connect costs 37 ms to aiplatform and 64 ms to api.typesafe.ai. The new process-wide
  connection pool reuses sockets across runs. `warm()` pre-opens a connection and pre-fetches the gcloud
  token off-thread.
* **Cost:** about $0.00022 per crop batched on 3.5-flash-lite, and $0.00036 for a single-crop call. The
  whole eval cost about $0.12 across roughly 180 API calls. That count includes probes and failures.

## What was built

| file | what |
| --- | --- |
| `mobile_agent/vision_judge.py` (new) | `VisionJudge`, `Judgment`, `CropJudgment`, `JudgeConfig`, `Step`, `eye_colour_steps`, `person_gate_steps`, `EYE_COLOUR_THRESHOLDS`, `crop_for_element`, `rect_to_pixels`, `encode_jpeg`, `LocalScreen`, `AppleVisionScreen`, `answer_probabilities`, `risk_coverage`, `fit_threshold`; `python -m mobile_agent.vision_judge build-t0` |
| `mobile_agent/vision_t0.swift` (new) | Long-lived Apple Vision helper: classify (1,303 labels), animals, face count. It reads length-prefixed frames on stdin and writes one JSON line per frame. It is built into `mobile_agent/.build/vision-t0`, which is gitignored. |
| `mobile_agent/http_pool.py` (new) | `ConnectionPool` and `PooledHTTP`: process-wide warm HTTP clients keyed by base URL and a SHA-256 of the key |
| `mobile_agent/gemini.py` | `VertexHTTP` now translates OpenAI-style `image_url` data-URL parts into `inlineData`, with per-image `detail` mapped to `mediaResolution`. It also accepts request-level `media_resolution`, a strict `json_schema` (sent as `responseJsonSchema`) and `logprobs`/`top_logprobs`. Logprobs come back OpenAI-shaped, and malformed logprobs are dropped. Text-only payloads are byte-identical to before (there is a test). The client is pooled, and `warm()` was added. |
| `mobile_agent/models.py` | `Jev` main and side channels are pooled, and `Jev.warm()` was added. `Helper.complete(..., response_format=, media_resolution=, logprobs=)` is new. OpenAI-compatible providers get resolution as a per-image `detail`. Also added: `Helper.supports_logprobs`, `Helper.warm()`, `Helper.close()`, `image_part()`, and a thread-safe call counter. The default request body is unchanged. |
| `mobile_agent/evals/vision/` | `manifest.json` (135 labelled items over 132 images, each with its source URL, licence and author), `images/` (512-px re-encodes, 5 MB), `run_eval.py` (live runner and summariser), `results/` (raw JSONL and per-call logs of every run in this memo) |
| tests | `test_vision_judge.py` (35), `test_http_pool.py` (13), `test_gemini_images.py` (10) |

### Interface

```python
from mobile_agent.vision_judge import VisionJudge, Judgment, crop_for_element, eye_colour_steps

judge = VisionJudge(helper_factory=None, config=None)          # default: models.Helper per model
crop = crop_for_element(still_png_or_jpeg, element.rect)         # normalized AX rect, 8% padding
j = judge.judge("Is there a real dog in this photo?", [crop], ("yes", "no", "unsure"), timeout=4)
j.answer, j.score, j.abstain_reason, j.tier, j.latency_ms, j.per_crop   # answer is always one of choices
judge.judge("What colour are this person's eyes?", profile_crops,
            ("blue", "green", "grey", "hazel", "brown", "unsure"), steps=eye_colour_steps(),
            aggregate="consensus", min_agree=2, hires=[full_res_crops])
judge.judge_many(question, [[cell] for cell in grid_cells])      # one batched request for a screen of cells
```

`choices` must include an abstention value (`unsure`, `unclear` or `unknown`). Malformed output, transport
errors, timeouts and unreadable images all abstain, and they never go to T2.

### Cascade mechanics

1. **T0** (`AppleVisionScreen`, only for a named `predicate=`) decides a crop when a rule holds. Otherwise the
   crop passes on. The rules are fitted on this set:
   * dog: animals Dog ≥ 0.6
   * receipt: label receipt ≥ 0.35
   * sunglasses: label sunglasses ≥ 0.3 and ≥ 1 face

   If the binary is missing (the desktop sidecar does not bundle it), T0 is skipped.
2. **T1**: crops are downscaled (≤ 512 px, q80), and up to 8 go in one request at `media_resolution=low`.
   Each image is labelled "Image k:". The strict JSON schema holds, per image, an enum answer for each step
   and a `<key>_confidence` for the final step. Gate steps are answer-only: their confidences were 0.95–0.99
   on every crop and doubled the output tokens. Where the model supports logprobs (`gemini-2.5-flash-lite`
   only; 3.5-flash-lite and 3.7-flash reject them with HTTP 400), the answer token's probability replaces the
   self-reported score.
3. **Gates**: an answer outside `gate` fails the gate. If it was confident, the crop gets `on_fail` (a
   choice) or abstains with the gate's reason. If the gate answer was `unsure`, the crop becomes a T2
   candidate. The chain score is `min` (default) or `product`.
4. **T2**: crops whose score falls in [0.5, threshold), or that were `unsure`, are sent again. The input is
   the caller's `hires` crop, or the original, at ≤ 1024 px and `media_resolution=high`, optionally on
   `t2_model`. A confident T2 result wins. Otherwise the crop abstains with both tiers' answers in the
   reason.
5. **Aggregation per entity**:
   * `consensus`: confident crops must agree, and no contradicting crop may be above the floor. `min_agree`
     is optional.
   * `any`: positive if any crop is confidently positive, negative only if every crop is conclusive.
   * `all`.
6. **Robustness**: requests are hedged; a failed request is retried once when ≥ 1 s remains; helpers are
   pooled per judge. A helper whose request timed out or errored is closed (its request is aborted) and never
   reused.

## The labelled set

* 132 CC0 or public-domain images from Wikimedia Commons, found through structured-data search
  (`haswbstatement:P275=Q6938433|P6216=Q19652`) and downloaded with a `User-Agent` string. The licence,
  author and source page are recorded per item from the Commons `extmetadata` API.
* I labelled every image by looking at contact sheets and zoomed crops. There was a single rater, and
  ambiguous candidates were excluded (drawings of dogs, blank receipt forms, a pulled-down pair of
  sunglasses).
* Nudity and lingerie shots returned by the search were excluded.

| predicate | question (verbatim in `manifest.json`) | positives | negatives / other | hard cases included |
| --- | --- | --- | --- | --- |
| dog | real dog, not a toy, statue, costume, robot, sign or drawing | 14 | 16 | tiny dogs in street and beach scenes, robot dogs, mascot costume, flower "Puppy" sculpture, dog-beach pictogram, foxes, wolf, cats, teddy bears |
| receipt | printed, handwritten or on-screen record of a purchase or payment | 20 | 20 | handwritten 1840s receipts, a mobile-payment receipt screenshot, crumpled thermal receipts; menus, letters, forms, newspapers, a deli price label |
| sunglasses | a person wearing sunglasses on their face | 14 | 14 | small far-away wearers; sunglasses product shots, clear eyeglasses, glasses held in the hand, busts and paintings |
| eye colour | blue / green / grey / hazel / brown / unsure | 24 answerable | 13 unanswerable | close-ups and portraits; B&W photos, profile or upturned faces, sunglasses, a cat and a dog; blue–grey and green–hazel ambiguity is scored with an accept set per item |

**Phone-like degradation.** T1 received each image at 320 px long side and JPEG quality 50. That stands in for
a photo cell in WDA's half-scale, quality-50 MJPEG. T2 received the stored 512-px image as the stand-in for a
full-resolution still. Commons photos are not phone screenshots, so a phone-crop set is still needed (see
Next).

## Results

### T1 alone (batch 8, `gemini-3.5-flash-lite`, low resolution)

| predicate | accuracy | errors (self-reported confidence) | notes |
| --- | --- | --- | --- |
| dog | 30/30 | — | |
| receipt | 39/40 | deli price label → "yes" (0.99) | |
| sunglasses (no person gate) | 27/28 | sunglasses product, no person → "yes" (1.0) | fixed by `person_gate_steps` |
| eye colour, decomposed, accept 0.8 | precision 90–91% (2/20, 3/24 wrong over two runs) | blue→green/brown (0.8), brown→hazel (0.9), upturned face → brown (0.9) | 0–1 of 13 unanswerable answered |
| eye colour, holistic, accept 0.8 | precision 81.5% (5/27 wrong) | as above, plus hazel for a B&W photo (0.7) and brown for a profile (0.8) | 3/13 unanswerable answered |

### Calibration

* **Binary predicates.** Self-reported confidence was 0.95–1.0 on almost every answer, right or wrong
  (lowest correct 0.8, 0.9 and 0.95; the wrong ones 0.99 and 1.0). No threshold separates them, so the fix
  is a better question (a gate), not a threshold. The default `accept` is 0.9, which sent the one 0.8 dog
  (a tiny dog on a beach) to T2, and T2 answered it correctly.
* **Eye colour.** Self-reported confidence does separate: every iris answer at ≥ 0.95 was right in both
  runs (15/15 and 14/14), and all three errors sat at 0.8–0.9. `EYE_COLOUR_THRESHOLDS` = 0.95 for every
  colour. This threshold was fitted in-sample.
* **Logprobs** (`gemini-2.5-flash-lite`, the only model tested that returns them):
  * The first answer token is almost always at p > 0.98.
  * The wrong receipt had p = 1.000.
  * The lowest correct binary answer was p = 0.866.
  * They flagged one uncertain eye (p = 0.445).
  * On this set they add little over self-report for binaries. 429 responses cut the eye-colour run short
    (32 of 37 crops lost), so eye-colour logprob calibration is untested.
* `fit_threshold()` and `risk_coverage()` implement the selective-prediction fit used here. Refit them per
  model and question on held-out phone crops before relying on a number.

### Final cascade (T0 + T1 + T2, calibrated)

| predicate | answered | correct | abstained | T0 / T1 / T2 decided | notes |
| --- | --- | --- | --- | --- | --- |
| dog | 30 | 30 | 0 | 9 / 20 / 1 | T2 rescued the tiny beach dog (T1 unsure) |
| receipt | 40 | 39 | 0 | 6 / 34 / 0 | price label remains |
| sunglasses | 28 | 28 | 0 | 5 / 23 / 0 | with person gate |
| eye colour | 19 | 19 | 18 (13 correct abstentions) | 0 / 27 / 10 | T2 rescued 5 answers. The remaining 5 abstentions include all three T1 errors. |

For the eye-colour row, the T2 column counts crops that went to T2, not answers T2 decided (it answered 5 and
abstained on 5). Abstention reasons are specific, for example:

* "no reliable colour information"
* "eyes not visible"
* "uncertain after look-again (T1 green 0.85, T2 hazel 0.90)"

Only one real multi-photo entity was in the set (two photos of the same woman), and consensus returned green
on both. The unit tests cover aggregation (agreement, disagreement, `min_agree`, `any`, `all`).

### Latency per tier (successful calls; the results folder holds every call)

| tier and shape | n | p50 | p90 |
| --- | --- | --- | --- |
| T0 Apple Vision, 320-px crop (compute / wall incl. IPC) | 135 | 6.5 / 8.5 ms | 8.6 / 11.5 ms |
| T0 at 512 px (wall) / cold start + first frame | 20 / 1 | 13.8 ms / 177 ms | — |
| T1, 1 crop, single question | 17 | 888 ms | 1,113 ms |
| T1, 1 crop, decomposed (4 steps) | 4 | 834 ms | 940 ms |
| T1, 6–8 crops, single question | 35 | 1,620 ms | 2,075 ms |
| T1, 8 crops, decomposed, answer-only gates | 8 | ≈2.1 s | 2.5 s |
| T1, 8 crops, decomposed, gate confidences (before) | 5 | 3.3 s | 4.9 s |
| T2 high resolution, 1 crop / 4–5 crops | 3 / 3 | 1.3–1.6 s / 1.9–2.3 s | — |
| T1 `gemini-2.5-flash-lite`, 8 crops (with logprobs) | 12 | 1,290 ms | 1,589 ms |

About output tokens and latency:

* Output tokens drive batch latency. Decomposed eye colour went from ~915 to ~495 output tokens per batch
  of 8 when the gate confidences were dropped, and from 3.3 s to 2.1 s.
* A single-question batch of 8 is ~280 output tokens.
* Input: 3.5-flash-lite used ~256 image tokens per crop at `low` (1,089 at default or high for a 512×512
  image). 2.5-flash-lite used about 66.

**Stalls and rate limits.**

* 3 of 24 single-crop requests and 1 batch hung until the 20 s transport deadline, and 1 batch failed after
  10 s. Nothing in the request distinguished them, and 24 small text requests (12 on a reused connection, 12
  fresh) all finished in 0.56–1.22 s. So the stall looks server-side on image requests, and it is not caused
  by connection reuse.
* Hedging bounds these stalls at about the hedge delay plus one normal call, roughly 2.3 s for one crop.
  During the calibrated run it fired twice, both times on T2.
* 429s: 13 in total, 12 of them on 2.5-flash-lite. The one retry recovered 2 of the 7 rate-limited batches.
  Sustained quota exhaustion needs backoff at a higher level, or a different model.

### Connections

| measurement | value |
| --- | --- |
| TCP+TLS connect from this Mac, api.typesafe.ai / aiplatform.googleapis.com | 64 / 37 ms p50 |
| Vertex text call, fresh connection vs reused (n = 12 each) | 676 vs 640 ms p50 |
| `gcloud auth print-access-token` (cache cold) | 474–738 ms |
| idle keep-alive closed by server | aiplatform 240 s; api.typesafe.ai > 900 s |

The pool keeps idle clients for 180 s (Vertex), 600 s (Jev) and 120 s (anything else). Before reuse it drops a
client whose socket is readable (the peer closed it or sent bytes) and closes clients that were mid-request.
Only a hash of each credential is kept. A Vertex client goes back to the pool with its bearer token cleared.

### Cost

| model | per crop (batch 8) | per single-crop call |
| --- | --- | --- |
| gemini-3.5-flash-lite (`costs.py` rates) | $0.00022 | $0.00036 |
| gemini-2.5-flash-lite (list price $0.10 / $0.40 per M) | $0.000024 | — |

Eval total ≈ $0.12.

## Recommendations

1. **Use VisionJudge only for `ax_image` predicates, with a named `predicate` when one exists, so T0 can
   confirm positives locally.** Always pass `hires` (a full-resolution still crop) for attribute questions.
   T2 rescued 5 of 10 eye crops here, from a 1.6× larger image.
2. **Decompose every attribute question about people and gate "wearing X" questions on a person.**
   Holistic eye colour invents colours for photos that do not have one.
3. **Batch per screen.** Eight crops cost 1.6 s against 0.9 s for one, so grids and all the photos of a
   profile should go in one request. For swipe decks, where only one photo is judged at a time, expect
   ~0.9 s, and ~2.2 s with T2.
4. **Wire warm-up at run start** (not done here, because `compose.py`, `agent.py` and `server.py` belong to
   others): `Jev.warm()`, `Helper.warm()` / `VisionJudge.warm()`. For Vertex this hides the ~0.5 s token
   fetch and the connect behind the first observation.
5. **Try `gemini-2.5-flash-lite` as T1** once quota allows. It was 20% faster and 9× cheaper per crop, it
   was equally accurate on the binary predicates, and it is the only one that returns logprobs. Its project
   quota returned 429 often.
6. **Dependency gap.** `crop_for_element` and resizing need Pillow, and Pillow is neither in
   `requirements.txt` nor in the frozen sidecar. JPEG bytes that need no resize pass through without it. The
   requirements owner should add `Pillow` if the sidecar is to crop.
7. **Privacy.** Crops of third parties go to Google at T1 and T2. T0 is local. Send minimal crops, and keep
   refusing predicates on protected characteristics. That policy lives in the loop compiler, not here.

## Caveats

* The labels come from one rater, and the images are Commons photographs, not phone screenshots. The T0
  rules and eye-colour threshold were fitted on the same set they are reported on, so the precision figures
  are in-sample.
* Sample sizes are small. Zero errors in 30 certifies only ≥ 90.5% precision, and zero in 19 only ≥ 85.4%
  (one-sided Clopper–Pearson bounds). Certifying 95% needs ≥ 59 positive decisions per predicate.
* Model latency drifts between batches and times of day. The p50/p90 come from ~100 calls over one hour.
* The eye-colour "unsure" class mixes true unanswerables (B&W, eyes hidden) with scoring choices (tiny faces
  accept "unsure" or "brown").

## Next

1. Record phone crops: Photos grid, a feed and a deck, with MJPEG, a full-resolution still and AX. Label
   300+ items per predicate with two raters, then refit `EYE_COLOUR_THRESHOLDS`, the T0 rules and `accept`
   per app.
2. Measure WDA `/screenshot` cost on the device to decide between a still and MJPEG "high" for T2 input.
3. Add a feature-print identity call to `vision_t0.swift` (2.8 ms measured earlier) for loop item identity.
4. Re-run the logprob comparison when 2.5-flash-lite quota allows, and consider it for T1.
