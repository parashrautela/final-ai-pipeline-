# Video generation — UI flow v1

Status: proposed UI specification, 4 November 2026. This describes a new flow; it is not app implementation or evidence of a working final-video renderer. The existing live pilot remains unchanged.

## Confirmed decisions

- Enter from a catalogue product and select 1–2 of that product's actual available images. Both references feed **one video**. Choose one **main image**; the second is a supporting view/detail of the **same product**.
- Choose **Product-led** or **Human-led**. Human-led adds model appearance choices and a generated wearing preview. Import the product category and use its correct jewellery placement. Ethnicity and skin tone are separate attributes; an adult model is the proposed default below.
- Both paths produce **one storyboard contact-sheet image with 9–10 panels**, using OpenAI image generation.
- The user must review and approve the storyboard before final video generation. They can change shot directions and regenerate first. Never continue automatically from storyboard generation to video generation.
- Generation calls are authorized for **OpenAI only**. No Kling, Nano Banana or other provider calls, production changes, or changes to the existing pilot are part of this task.

## Proposed screens and actions

| Screen | Content and interaction | Primary action | Secondary actions |
| --- | --- | --- | --- |
| Product / source selection | Show this product's available images and selected count, maximum two. Mark one selected image as Main and the other as Supporting. | Continue | Mark as main; remove selection; expand image |
| Direction and duration | Product-led or Human-led. Show durations supported by the new backend contract. Choose duration before storyboard generation so the shot plan can match it. | Continue | Back |
| Human setup — Human-led only | Adult model appearance; separate ethnicity and skin-tone fields; imported category and proposed body placement. Generate a wearing image using the product references. | Create wearing preview | Back; change appearance |
| Wearing preview — Human-led only | Compare the jewellery with the original reference; inspect body placement, model appearance and product fidelity. Select the wearing scene to carry forward. | Use this wearing preview | Expand; change setup; regenerate |
| Storyboard | Generate one 9–10-panel preview asset. Show it alongside the original main image and supporting image, plus the selected wearing preview for Human-led. Panel numbers map to editable shot directions. | Approve storyboard | Tap to expand; edit shot directions; regenerate; back |
| Final review | Show current approved storyboard version, source/main selection, direction, human setup if applicable, duration and a fresh video quote. | Generate video · [quoted cost] | Edit storyboard; back |
| Progress | Show queued, generating and saving states from the backend. Explain that leaving the screen preserves the job. | View progress | Close; cancel only when the backend permits |
| Result and history | Play the completed video; show its source thumbnail, date, duration, status and approved storyboard. Find previous videos from this product. | Play video | Download / Save or share; view previous videos |

A central cross-product library is a separate proposed extension. Per-product history is the first scope.

## Reference and selection behavior

- Display the actual product sources dynamically, rather than assuming four images exist. Enable Continue only with 1–2 valid selections and exactly one Main. With one image, show it as Main; with two, provide an explicit **Mark as main** action. Removing Main requires choosing a new Main before continuing.
- Adding a supporting image must not replace Main silently. Preserve both roles when moving between screens.
- Main always anchors jewellery identity. For Product-led, it also anchors the scene/composition. Supporting supplies visible construction/detail information; it must never fuse two designs or redesign the jewellery.
- Human-led intentionally creates a new wearing scene; it need not retain the catalogue display, background or literal first frame. The selected wearing preview then anchors the human, styling and scene across the storyboard.
- Category determines placement, such as earrings on ears or a necklace around the neck. Unknown/ambiguous categories require correction before generating a wearing preview. Default to an adult model; do not infer ethnicity from skin tone.
- Product fidelity and the same human across shots are review goals, not guarantees. Prefer views supported by the supplied references; do not promise accurate unseen geometry.

## Storyboard versions, approval and quotes

Each storyboard has a version tied to its source images, Main assignment, Product-led/Human-led choice, human appearance and selected wearing preview, duration, generation configuration and shot directions.

Changing any of those inputs marks the affected preview/storyboard stale, clears its approval and invalidates the video quote. Source or human-setup changes also invalidate a dependent wearing preview. Editing shot directions requires regenerating the storyboard and approving the new version; approval never carries to a replacement asset.

**Approve storyboard** records approval of the displayed version. **Generate video** requires that same current version to be approved and a valid quote for it. Expired quotes refresh without regenerating an unchanged approved storyboard. Prevent duplicate submission; recover uncertain requests with the original request identity.

Duration-before-storyboard is a proposed UI default. The existing pilot supports **5/10 seconds only**; **4/8 seconds are future requests**, not supported choices promised by this spec. Changing duration after approval requires a new matching storyboard and quote.

## Required states and backend dependencies

- Handle no usable images, source removal, loading, wearing/storyboard generation failure, stale preview, unapproved storyboard, quote failure/expiry and unavailable generation without discarding the user's choices.
- Distinguish an uncertain submitted request from a confirmed failure. Retry/recover the existing attempt safely. Refresh unavailable media for an existing result rather than generating a replacement.
- The existing API accepts one `image_index`, restricted to 0–3. Two references, explicit Main, human setup, storyboard versions/approval and selection beyond the fourth image require backend additions. A dynamic UI must respect server-advertised source eligibility; it cannot make a fifth source work against the legacy API.
- The backend must provide OpenAI generation contracts, category/placement rules, supported durations, preview storage, storyboard-to-shot mapping, approval validation, quotes, job states and owned history/media access.
- A contact-sheet preview is not proof that a video renderer can consume a collage. Final rendering requires an explicit input/shot contract that respects the approved storyboard. Until that integration exists, disable live final submission or use a clearly labelled mock during UI testing.

## Focused acceptance criteria

1. One or two actual images from the same catalogue product produce one video request with an unambiguous Main; a third selection is blocked.
2. Human-led exposes separate ethnicity/skin-tone controls, imports category and resolves correct placement before its wearing preview; Product-led skips these screens.
3. Both paths show one expandable 9–10-panel storyboard asset, original references and editable shot directions. Final submission is unavailable until explicit approval.
4. Relevant input changes invalidate dependent approval/quote; only the current approved version can be submitted with a valid quote.
5. Users can leave and return to a job, find prior product videos, play a result and download/save/share it. Recovery does not create a duplicate attempt.
6. Live controls reflect implemented backend capabilities. Mock states are labelled; this specification triggers no generation calls or changes to the existing live pilot.
