#!/usr/bin/env node

import { existsSync } from "fs";
import { homedir } from "os";
import { join } from "path";
import { pathToFileURL } from "url";

function option(name, fallback = null) {
  const index = process.argv.indexOf(name);
  return index >= 0 && index + 1 < process.argv.length ? process.argv[index + 1] : fallback;
}

function fail(message, code = 1) {
  process.stderr.write(`${message}\n`);
  process.exit(code);
}

const command = process.argv[2];
if (!command || !["list", "get", "transcript"].includes(command)) {
  fail("usage: voice_memo_cli.mjs list | get --id ID | transcript --id ID [--language en-US]", 2);
}

const codexHome = process.env.CODEX_HOME || join(homedir(), ".codex");
const toolRoot = process.env.VOICE_MEMO_MCP_DIR || join(codexHome, "tools", "apple-voice-memo-mcp");
const dist = join(toolRoot, "dist");
if (!existsSync(join(dist, "services", "voice-memo-db.js"))) {
  fail(`pinned Voice Memos runtime is not built: ${toolRoot}`);
}

const load = (relativePath) => import(pathToFileURL(join(dist, relativePath)).href);
const { VoiceMemoDatabase } = await load("services/voice-memo-db.js");
const database = new VoiceMemoDatabase();

try {
  if (command === "list") {
    const first = database.listMemos({ limit: 1, offset: 0 });
    const result = database.listMemos({ limit: Math.max(first.total, 1), offset: 0 });
    process.stdout.write(`${JSON.stringify({ memos: result.memos, total: result.total })}\n`);
  } else {
    const rawID = option("--id");
    const id = Number.parseInt(rawID ?? "", 10);
    if (!Number.isInteger(id)) fail("--id must be an integer", 2);
    const memo = database.getMemo(id);
    if (!memo) fail(`Voice Memo ${id} was not found`, 3);

    if (command === "get") {
      process.stdout.write(`${JSON.stringify(memo)}\n`);
    } else {
      const { TranscriptExtractor } = await load("services/transcript-extractor.js");
      const embedded = new TranscriptExtractor().extractTranscript(memo.path, "text");
      if (embedded?.text?.trim()) {
        process.stdout.write(`${JSON.stringify({ id, source: "embedded", text: embedded.text.trim(), locale: embedded.locale })}\n`);
      } else {
        const { TranscriptionService } = await load("services/transcription-service.js");
        const helper = process.env.VOICE_MEMO_TRANSCRIBER_PATH || join(toolRoot, ".codex-build", "VoiceMemoTranscriber");
        const result = await new TranscriptionService(helper).transcribe(memo.path, option("--language", "en-US"));
        if (!result.success || !result.transcript?.trim()) {
          fail(result.error || `Voice Memo ${id} produced an empty transcript`, 4);
        }
        process.stdout.write(`${JSON.stringify({ id, source: "apple-speech", text: result.transcript.trim() })}\n`);
      }
    }
  }
} finally {
  database.close();
}
