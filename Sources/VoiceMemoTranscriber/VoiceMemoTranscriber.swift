import AVFoundation
import CoreMedia
import FluidAudio
import Foundation
import Speech

enum TranscriberError: Error, CustomStringConvertible {
    case emptyTranscript
    case unsupportedLocale(String)

    var description: String {
        switch self {
        case .emptyTranscript:
            return "SpeechAnalyzer returned an empty transcript"
        case .unsupportedLocale(let locale):
            return "Unsupported locale: \(locale)"
        }
    }
}

struct TimedText {
    let text: String
    let start: Double
    let end: Double
}

struct SpeakerSpan {
    let id: String
    let start: Double
    let end: Double
}

struct TranscriptSegment: Encodable {
    var text: String
    var start: Double
    var end: Double
    var speaker: String
}

struct Transcription {
    let text: String
    let timedText: [TimedText]
}

struct TranscriberOutput: Encodable {
    let text: String
    let segments: [TranscriptSegment]
    let speakerCount: Int
    let diarizationStatus: String
}

struct DiarizationStatus: Encodable {
    let available: Bool
    let modelsDirectory: String
}

@main
struct VoiceMemoTranscriber {
    static func main() async {
        if CommandLine.arguments.contains("--help") {
            print("Usage: VoiceMemoTranscriber <audio-file> [--language en-US] [--json]")
            print("       VoiceMemoTranscriber --prepare-diarization | --diarization-status")
            return
        }

        do {
            if CommandLine.arguments.contains("--prepare-diarization") {
                let manager = OfflineDiarizerManager()
                try await manager.prepareModels()
                try printJSON(diarizationStatus())
                return
            }
            if CommandLine.arguments.contains("--diarization-status") {
                let status = diarizationStatus()
                try printJSON(status)
                if !status.available {
                    exit(3)
                }
                return
            }

            guard CommandLine.arguments.count >= 2 else {
                fputs("Usage: VoiceMemoTranscriber <audio-file> [--language en-US] [--json]\n", stderr)
                exit(1)
            }

            let audioPath = CommandLine.arguments[1]
            let languageIndex = CommandLine.arguments.firstIndex(of: "--language")
            let language = languageIndex.flatMap { index in
                CommandLine.arguments.indices.contains(index + 1) ? CommandLine.arguments[index + 1] : nil
            } ?? "en-US"

            let transcription = try await transcribe(audioPath: audioPath, language: language)
            var spans: [SpeakerSpan] = []
            var status = "unavailable"
            do {
                spans = try await diarize(audioPath: audioPath)
                status = spans.isEmpty ? "no-speaker-segments" : "complete"
            } catch {
                // Speech transcription remains useful when the optional local
                // diarization model is unavailable or cannot analyze a clip.
                spans = []
            }
            let segments = align(transcription.timedText, with: spans)
            let speakerCount = max(1, Set(segments.map(\.speaker)).count)
            try printJSON(TranscriberOutput(
                text: transcription.text,
                segments: segments,
                speakerCount: speakerCount,
                diarizationStatus: status
            ))
        } catch {
            fputs("Error: \(error)\n", stderr)
            exit(1)
        }
    }

    static func printJSON<T: Encodable>(_ value: T) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        print(String(decoding: try encoder.encode(value), as: UTF8.self))
    }

    static func diarizationStatus() -> DiarizationStatus {
        let directory = OfflineDiarizerModels.defaultModelsDirectory()
        let required = ModelNames.OfflineDiarizer.requiredModels
        let discovered = Set(
            (FileManager.default.enumerator(at: directory, includingPropertiesForKeys: nil)?
                .compactMap { ($0 as? URL)?.lastPathComponent }) ?? []
        )
        let available = required.isSubset(of: discovered)
        return DiarizationStatus(available: available, modelsDirectory: directory.path)
    }

    static func transcribe(audioPath: String, language: String) async throws -> Transcription {
        let requestedLocale = Locale(identifier: language)
        guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: requestedLocale) else {
            throw TranscriberError.unsupportedLocale(language)
        }

        let transcriber = SpeechTranscriber(
            locale: locale,
            transcriptionOptions: [],
            reportingOptions: [],
            attributeOptions: [.audioTimeRange]
        )
        if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
            try await request.downloadAndInstall()
        }

        let analyzer = SpeechAnalyzer(modules: [transcriber])
        let resultTask = Task<Transcription, Error> {
            var textParts: [String] = []
            var timedText: [TimedText] = []
            for try await result in transcriber.results {
                let resultText = String(result.text.characters).trimmingCharacters(in: .whitespacesAndNewlines)
                if !resultText.isEmpty {
                    textParts.append(resultText)
                }
                var foundTimedRun = false
                for run in result.text.runs {
                    let runText = String(result.text[run.range].characters)
                    guard !runText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                        let timeRange = run[AttributeScopes.SpeechAttributes.TimeRangeAttribute.self]
                    else {
                        continue
                    }
                    let start = CMTimeGetSeconds(timeRange.start)
                    let duration = CMTimeGetSeconds(timeRange.duration)
                    guard start.isFinite, duration.isFinite, duration >= 0 else {
                        continue
                    }
                    timedText.append(TimedText(text: runText, start: start, end: start + duration))
                    foundTimedRun = true
                }
                if !foundTimedRun, !resultText.isEmpty {
                    let start = CMTimeGetSeconds(result.range.start)
                    let duration = CMTimeGetSeconds(result.range.duration)
                    if start.isFinite, duration.isFinite, duration >= 0 {
                        timedText.append(TimedText(text: resultText, start: start, end: start + duration))
                    }
                }
            }
            let text = joinText(textParts)
            return Transcription(text: text, timedText: timedText)
        }

        let audioFile = try AVAudioFile(forReading: URL(fileURLWithPath: audioPath))
        if let lastSampleTime = try await analyzer.analyzeSequence(from: audioFile) {
            try await analyzer.finalizeAndFinish(through: lastSampleTime)
        } else {
            await analyzer.cancelAndFinishNow()
        }

        let transcription = try await resultTask.value
        guard !transcription.text.isEmpty else {
            throw TranscriberError.emptyTranscript
        }
        return transcription
    }

    static func diarize(audioPath: String) async throws -> [SpeakerSpan] {
        let manager = OfflineDiarizerManager()
        try await manager.prepareModels()
        let result = try await manager.process(URL(fileURLWithPath: audioPath))
        return result.segments.compactMap { segment in
            let start = Double(segment.startTimeSeconds)
            let end = Double(segment.endTimeSeconds)
            guard start.isFinite, end.isFinite, end > start else {
                return nil
            }
            return SpeakerSpan(id: segment.speakerId, start: start, end: end)
        }.sorted { left, right in
            left.start == right.start ? left.end < right.end : left.start < right.start
        }
    }

    static func align(_ timedText: [TimedText], with spans: [SpeakerSpan]) -> [TranscriptSegment] {
        guard !timedText.isEmpty else {
            return []
        }
        var rawSpeakerOrder: [String] = []
        var assigned: [(TimedText, String)] = []
        for item in timedText {
            let speaker = bestSpeaker(for: item, spans: spans) ?? "single-speaker"
            if !rawSpeakerOrder.contains(speaker) {
                rawSpeakerOrder.append(speaker)
            }
            assigned.append((item, speaker))
        }
        let labels = Dictionary(uniqueKeysWithValues: rawSpeakerOrder.enumerated().map {
            ($0.element, "Speaker \($0.offset + 1)")
        })
        var result: [TranscriptSegment] = []
        for (item, rawSpeaker) in assigned {
            let label = labels[rawSpeaker] ?? "Speaker 1"
            if var previous = result.last, previous.speaker == label {
                previous.text = joinText([previous.text, item.text])
                previous.end = max(previous.end, item.end)
                result[result.count - 1] = previous
            } else {
                result.append(TranscriptSegment(
                    text: item.text.trimmingCharacters(in: .whitespacesAndNewlines),
                    start: item.start,
                    end: item.end,
                    speaker: label
                ))
            }
        }
        return result.filter { !$0.text.isEmpty }
    }

    static func bestSpeaker(for item: TimedText, spans: [SpeakerSpan]) -> String? {
        var overlapBySpeaker: [String: Double] = [:]
        for span in spans {
            let overlap = max(0, min(item.end, span.end) - max(item.start, span.start))
            if overlap > 0 {
                overlapBySpeaker[span.id, default: 0] += overlap
            }
        }
        if let best = overlapBySpeaker.max(by: { left, right in
            left.value == right.value ? left.key > right.key : left.value < right.value
        }) {
            return best.key
        }
        let midpoint = (item.start + item.end) / 2
        return spans.min(by: { distance(to: $0, from: midpoint) < distance(to: $1, from: midpoint) })?.id
    }

    static func distance(to span: SpeakerSpan, from time: Double) -> Double {
        if time < span.start { return span.start - time }
        if time > span.end { return time - span.end }
        return 0
    }

    static func joinText(_ parts: [String]) -> String {
        var result = ""
        let closingPunctuation = CharacterSet(charactersIn: ".,?!:;)]}%")
        for rawPart in parts {
            let part = rawPart.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !part.isEmpty else { continue }
            if result.isEmpty {
                result = part
            } else if let first = part.unicodeScalars.first, closingPunctuation.contains(first) {
                result += part
            } else {
                result += " " + part
            }
        }
        return result
    }
}
