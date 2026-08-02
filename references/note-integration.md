# Note Integration

Interpret the memo in the context of the vault; do not force one memo into one new file.

Only integrate transcripts accepted by `qualify_transcript.py`. Qualification requires one of the configured phrases such as `work note`, `for work`, `work memo`, or `note for work`. The phrase is an opt-in control, not note content.

Use the generated Voice Memos title as a retrieval label, not as evidence or mandatory note text. It should describe the memo's central subject and may inform a new note title when the vault context supports that choice.

1. Load capped `.cursor/rules/foam-notes.mdc` and `.foam/templates` guidance.
2. Build a capped path/heading vault map, prioritizing retrieved candidates, then score eligible note bodies for people, projects, decisions, topics, and dates mentioned in the transcript. Weight rare, specific entities across the vault more strongly than generic recurring words.
3. Run `scripts/resolve_journal_date.py --recorded-at <recorded-at>` and include the resolved journal among up to five content candidates.
4. Choose the smallest coherent change:
   - append to an existing topical note when the memo extends it;
   - add a dated entry to the resolved journal when it is temporal or reflective;
   - create an atomic, concept-oriented dated note when the content forms a durable standalone topic;
   - update multiple notes only when cross-links materially improve retrieval.
5. Return a structured additive edit plan. The deterministic coordinator validates paths, applies it, and preserves all existing content. Add concise facts, decisions, and ideas. Add an action item only when the speaker explicitly states a task, request, commitment, reminder, or next step. Never convert a preference, uncertainty, expectation, observation, or implied possibility into a checkbox, and do not invent follow-up work. Do not paste the raw transcript.
6. Add exactly one provenance marker somewhere in the integrated content:

```html
<!-- voice-memo-id:123 -->
```

7. Use standard Foam `[[wikilinks]]` when linking related notes.

Treat the journal as a capture layer and the rest of the vault as an associative knowledge graph. A new non-journal note must never be orphaned: connect it directly to at least one existing eligible note, either with an outbound wikilink or an inbound link added to an existing candidate in the same plan. Every automated wikilink must resolve unambiguously to an existing eligible note or another note created in the same plan. Do not create speculative placeholders.

Map, index, and MOC-style notes are navigation surfaces. Add a concise link when a new durable note materially belongs in one, but keep the substantive content in the atomic note and do not duplicate it into the map.

Before any edit, search for the exact marker for the memo ID. One existing marker means the note was already integrated and state should be recovered from Git instead of creating duplicate content. More than one marker is an actionable integrity failure.

For journal routing, treat the helper's `journal_date` as authoritative. Saturday and Sunday recordings belong to the following Monday's journal, never a weekend journal. This rule does not prevent appending the memo to an existing topical note when that is the better destination.

Never read `mac-recorder-transcriber.md` as guidance for this workflow. Never modify `.cursor`, `.foam`, `.dotfiles`, `attachments`, `assets`, `images`, or the skill itself.
Files or directories containing `transcript` are excluded from both the vault map and candidate retrieval. If supplied context cannot support a topical placement, set confidence to low and write only to the authoritative journal.
