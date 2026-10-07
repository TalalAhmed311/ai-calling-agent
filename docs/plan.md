# Plan: Low-Latency Cached-Audio Calling Agent

**Status:** draft  
**Goal:** minimize perceived + end-to-end latency by streaming pre-recorded audio clips, with an LLM used only as a **decision boundary** (route/select), not as the primary speech generator.

---

## 1. Problem with the current shape

Today the hot path is roughly:

`Telnyx media ↔ Deepgram Voice Agent (STT + LLM + TTS)`

That is flexible, but every turn pays:

1. STT partials / final
2. LLM think time
3. TTS synthesis / first-byte
4. Network hops (Telnyx ↔ us ↔ Deepgram)

For a **narrow outbound sales script** (owner/manager gate → 2-minute ask → interest → callback time), most utterances are **predictable**. Generating them live is wasted latency.

**Staff take:** treat speech like a CDN problem, treat the LLM like a router.

---

## 2. Design thesis

> **Play cached audio by default. Use the LLM only to decide *which* clip (or small clip chain) to play next, and when to fall back to live TTS.**

Split the system into three planes:

| Plane | Responsibility | Latency budget |
|------|----------------|----------------|
| **Media plane** | Telnyx WS in/out, paced µ-law/a-law frames, barge-in clear | < 20–40ms local |
| **Decision plane** | Classify user turn → next node / clip IDs | < 150–400ms typical |
| **Speech plane** | Serve prebuilt telephony audio; live TTS only as escape hatch | **0ms synth** for cache hits |

The LLM is **not** in the audio loop unless cache miss / free-form answer is required.

---

## 3. Call graph (state machine first, LLM second)

Model `script.txt` as an explicit graph, not a free-form prompt.

```text
START
  └─ play: greeting_identity
      └─ listen → classify role
           ├─ owner|manager → play: ask_two_minutes → listen → classify availability
           │                      ├─ yes_now → play: short_pitch → play: ask_interest → listen
           │                      │                ├─ interested → play: ask_callback_time → listen → capture time → play: confirm_callback → END
           │                      │                └─ not_interested → play: polite_exit → END
           │                      ├─ busy → play: ask_better_time → listen → capture → play: confirm_callback → END
           │                      └─ not_interested → play: polite_exit → END
           ├─ neither → play: ask_owner_manager_time → listen → capture → play: thanks_callback → END
           ├─ who_is_this → play: identify_company → (re-ask role)
           └─ machine|ivr|voicemail → play?: vm_drop (optional) → END
```

**Rules:**
- Every node has 1..N **named clips**.
- Transitions are **labels**, not prose.
- LLM (or smaller classifier) outputs only: `{intent, slots, next_node, confidence}`.
- If confidence < threshold → one short probe clip, or live TTS fallback.

---

## 4. Audio cache architecture

### 4.1 Clip inventory

Build a versioned library at deploy/warmup time:

```text
audio_cache/
  v1/
    mulaw_8k/
      greeting_identity.raw
      ask_two_minutes.raw
      short_pitch.raw
      ask_interest.raw
      ask_callback_time.raw
      confirm_callback_generic.raw
      polite_exit.raw
      ask_owner_manager_time.raw
      identify_company.raw
      still_there.raw
      vm_drop.raw
    alaw_8k/
      ... same keys ...
    manifest.json
```

`manifest.json` holds: clip_id, text, duration_ms, hash, encoding, sample_rate, script_version.

### 4.2 Synthesis policy

- **Offline / startup:** synthesize all script lines once (Deepgram Aura / preferred TTS) into raw telephony PCM (mulaw + alaw @ 8k).
- **Never** synthesize the happy-path lines during a live call.
- **Slot fills** (callback day/time, names): prefer
  1. **phrase concatenation** of canned fragments (`Perfect_I'll_call_you` + `tomorrow` + `at` + `three_pm`), or
  2. **one short live TTS** only for the variable tail.
- Keep clip boundaries at natural phrase ends so joins don’t sound robotic.

### 4.3 Playback engine (media plane)

- Stream in **20ms frames** (160 bytes @ 8-bit 8kHz) — already the right unit for Telnyx.
- Maintain `play_queue: deque[ClipRef | LivePcm]`.
- Support:
  - `play(clip_id)`
  - `clear()` on barge-in
  - `prefetch(next_likely_clip_ids)` into memory
- Hold mic closed (or ignore inbound for decision) until mandatory opener finishes, unless barge-in allowed on that node.

**Latency win:** first audio sample after answer ≈ AMD wait + disk/memory read, not TTS RTT.

---

## 5. Decision boundary (LLM on top)

### 5.1 What the LLM sees

Per user turn (after STT final or high-confidence partial):

```json
{
  "node": "await_role",
  "transcript": "Yeah I'm the owner",
  "recent_clips": ["greeting_identity"],
  "allowed_intents": ["owner", "manager", "neither", "who_is_this", "not_interested", "machine", "unclear"]
}
```

### 5.2 What the LLM returns

Strict JSON only (schema-validated):

```json
{
  "intent": "owner",
  "confidence": 0.92,
  "slots": {},
  "next_node": "ask_two_minutes",
  "play": ["perfect_thanks", "ask_two_minutes"],
  "end_call": false,
  "fallback_tts": null
}
```

### 5.3 Model tiering (important)

Don’t use a heavy multimodal voice-agent stack for routing:

1. **Primary (fast):** small classifier / constrained LLM (flash-lite class) with JSON schema  
2. **Optional:** embedding + intent prototypes for ultra-common phrases (“speaking”, “owner”, “not interested”) → **skip LLM entirely**  
3. **Fallback:** larger model only on low confidence  
4. **Escape hatch:** `fallback_tts` text when no clip fits

**Staff rule:** LLM decides; cache speaks. If the LLM is speaking every turn, the architecture failed.

### 5.4 STT choice

Keep streaming STT (Deepgram listen / Flux), but **detach** it from bundled Agent TTS:

- Inbound audio → STT only  
- Decision plane → intent  
- Outbound → cache / rare live TTS  

This removes the Deepgram Agent “full duplex brain” from the critical audio path.

---

## 6. Runtime sequence (target hot path)

```text
call.answered
  → start media stream (parallel)
  → play greeting_identity from RAM cache   // no TTS wait
  → open listen

user speaks
  → streaming STT partials (optional early intent)
  → on endpointing: decision_plane.classify()
  → play queued clips immediately
  → repeat until END / hangup
```

**Target timings (aspirational):**

| Event | Budget |
|------|--------|
| Answer → first greeting sample | < 100–250ms after media up |
| User end-of-utterance → first response sample (cache hit) | < 200–500ms |
| Cache miss live TTS first byte | < 400–800ms |
| LLM decision alone | < 150–400ms |

---

## 7. Service layout (proposed)

```text
outbound_agent/
  media/          # Telnyx WS, frame pacing, barge-in
  cache/          # load manifest, clip store, prefetch
  graph/          # nodes, transitions, script compiler from script.txt
  decide/         # STT client, intent router, LLM JSON schema
  tts/            # offline builder + rare live synth
  amd/            # machine detection policy
  api/            # /call, /health, webhooks
```

**Process model:**
- Single asyncio worker per host is fine initially (current style).
- Keep clip bytes in process memory (mmap or dict).
- Warm TLS + STT websocket on `call.answered` (keep), but **don’t** wait on Agent Settings to speak.

---

## 8. Script → graph compiler

`script.txt` stays human-editable. Build step (or startup) compiles it to:

- `nodes.json` (ids, listen?, barge_in?, allowed_intents)
- `clips.json` (id → text)
- Generates/validates audio cache completeness

CI fails if a node references a missing clip.

This prevents prompt drift from breaking latency guarantees.

---

## 9. Observability (latency as a product metric)

Log per call:

- `t_answer`, `t_media_start`, `t_first_outbound_byte`
- `t_user_final` → `t_decision` → `t_first_response_byte`
- `cache_hit_rate`, `llm_skip_rate` (regex/embedding hits)
- `barge_in_count`, `fallback_tts_count`

Dashboards should optimize **time-to-first-audio**, not model quality alone.

---

## 10. Migration plan (from current agent)

### Phase 0 — keep working system
Current Telnyx + Deepgram path remains fallback.

### Phase 1 — cache playback hard path
- Expand `GREETING_CACHE` → full clip library for `script.txt`
- Play opener + next canned lines from cache
- Still use Deepgram Agent for decisions/speech if needed

### Phase 2 — split decision from speech
- STT-only stream
- LLM JSON router
- Cache player owns outbound audio
- Live TTS only for slots / unknown

### Phase 3 — fast-path without LLM
- Phrase matchers for top intents
- LLM only on ambiguous turns

### Phase 4 — graph compiler + CI audio bake
- Script changes produce new cache version atomically

---

## 11. Ambiguity, machine/bot detection, timers & edge cases

### 11.1 Ambiguous user reply (LLM decision fallback)

Decision output always includes `confidence`. Do **not** invent a path when unsure.

| Confidence | Action |
|------------|--------|
| High (≥ ~0.85) | Play next cached clip(s) for that intent |
| Medium | One short **probe** clip only (e.g. “Sorry — are you the owner or a manager?”) |
| Low / still unclear after **1 probe** | Soft-exit clip → hang up |

Rules:
- LLM may only choose among **`allowed_intents` for the current node**.
- If nothing fits → intent `unclear` (never free-form pitch).
- Max **2 probes per node**, then exit. Do not loop forever.

### 11.2 Bot / IVR / automated line (AMD ≠ VAD)

**VAD** = is someone speaking? (endpointing / turn-taking)  
**AMD / machine detect** = is it a human vs voicemail / IVR / carrier bot?

Use stacked detection — drop fast, never pitch machines:

1. **Telnyx premium AMD** (network-level)  
   - `machine` / fax → voicemail drop (optional) or immediate hang up  
   - Keep late-AMD abort if machine is detected after we already started human path  

2. **STT content classifier** (script / intent `machine`)  
   - Clear IVR: “press one”, “for sales press…”, “para español…”  
   - Clear voicemail: “leave a message after the tone/beep”, “you have reached the voicemail of…”  
   - Carrier error: “no longer in service”, “cannot be completed as dialed”  
   - → `end_call` immediately; disposition `IVR_OR_MACHINE`  

3. **Optional timing heuristic**  
   - Long unbroken “perfect” speech, no barge-in, menu-loop patterns → one probe, then treat as machine if no human reply  

**Not sufficient alone:** ringing, hold music, or “please hold” during a transfer.

### 11.3 Timers — when we fully drop the call

**Current code (baseline):**

| Timer | Value | Meaning |
|-------|-------|---------|
| Ring / dial timeout | 60s | No answer → Telnyx ends dial |
| AMD wait before assuming human | 0.35s | Optimistic; late AMD can still abort |
| Silence before “still there?” | 60s | First keep-alive prompt |
| Max silence prompts | 2 | Then hang up |
| `end_call` grace | ~3s | Let last sentence finish |

**Target policy for this sales script (recommended):**

| Edge | Drop after |
|------|------------|
| No answer (ringing) | **30–45s** (tighter than 60s) |
| Answered, never speaks | **20–25s** → one probe → **15s** more → hang up |
| Ambiguous replies | After **2 probes** on the same node → polite exit |
| Machine / IVR (high confidence) | **Immediate** hang up (no pitch) |
| Max total connected talk time | **3–4 min** hard cap |
| After `end_call` decision | **2–3s** then hang up |

Hard cap always wins: even a “good” call should not run unbounded.

### 11.4 Edge cases to encode in the graph

| Case | Behavior |
|------|----------|
| Gatekeeper / not owner-manager | Ask best time to reach owner/manager only → end (no pitch) |
| “Who is this?” / “What company?” | Play identify clip → re-ask role |
| Barge-in mid-clip | `clear` playback → re-decide on new transcript |
| Late AMD = machine during pitch | Abort mid-sentence → vm or hang up |
| STT empty / garbage | Probe once; do not advance graph |
| Callback time mumbled | One confirm clip; accept or retry **once** |
| Hostile / “don’t call again” | Confirm → `do_not_call` → end |
| Prospect busy mid-pitch | Jump to “better time?” node; don’t finish pitch |

**Principle:** cache plays audio; LLM only routes; ambiguity = probe then exit; bots = AMD + transcript rules and drop fast.

---

## 12. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Robotic clip joins | Phrase-level clips; crossfade optional; slot TTS for variables |
| Wrong intent → wrong clip | Confidence threshold + probe clip; easy undo with barge-in |
| Script changes need resynth | Versioned cache; bake on deploy |
| Over-caching free-form sales talk | Keep pitch to 1–2 fixed clips; don’t cache the whole universe |
| AMD / IVR | Keep Telnyx AMD + machine intents; don’t pitch machines |
| International audio issues | Always frame-paced 20ms; match negotiated PCMA/PCMU |
| Ambiguous loops | Max 2 probes/node then exit (see §11) |
| Runaway call length | 3–4 min hard talk-time cap |

---

## 13. Decision record (recommended)

**Adopt: Cached-audio state machine + LLM decision boundary.**

Do **not** pursue “fully generative voice agent on every turn” for this sales script. Flexibility is lower-value than:

- instant first speech
- predictable flow
- cheap/reproducible QA
- controllable compliance (exact sales wording)

Use generative TTS only at the edges: personalization, unexpected questions, voicemail variants.

---

## 14. Immediate next steps (when implementing)

1. Extract clip list from current `script.txt` (10–20 lines max).  
2. Add `audio_cache/` builder + in-memory loader.  
3. Implement `PlayQueue` on Telnyx media WS (20ms frames, clear on barge-in).  
4. Define intent schema for Step 1–4 only (include `unclear` + `machine`).  
5. Route owner/manager branch with cache only; measure `t_first_response_byte`.  
6. Encode §11 timers + probe limits in the graph runtime.  
7. Only then detach Deepgram Agent TTS from the hot path.

---

## 15. One-line architecture slogan

**Record once, stream forever; let the LLM choose the next buffer — not invent the waveform.**
