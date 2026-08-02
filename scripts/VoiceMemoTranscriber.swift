import AVFoundation
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

@main
struct VoiceMemoTranscriber {
    static func main() async {
        if CommandLine.arguments.contains("--help") {
            print("Usage: VoiceMemoTranscriber <audio-file> [--language en-US] [--json]")
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

        do {
            let text = try await transcribe(audioPath: audioPath, language: language)
            let output: [String: Any] = ["text": text, "segments": []]
            let data = try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys])
            print(String(decoding: data, as: UTF8.self))
        } catch {
            fputs("Error: \(error)\n", stderr)
            exit(1)
        }
    }

    static func transcribe(audioPath: String, language: String) async throws -> String {
        let requestedLocale = Locale(identifier: language)
        guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: requestedLocale) else {
            throw TranscriberError.unsupportedLocale(language)
        }

        let transcriber = SpeechTranscriber(locale: locale, preset: .transcription)
        if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
            try await request.downloadAndInstall()
        }

        let analyzer = SpeechAnalyzer(modules: [transcriber])
        let resultTask = Task<String, Error> {
            var parts: [String] = []
            for try await result in transcriber.results {
                let text = String(result.text.characters).trimmingCharacters(in: .whitespacesAndNewlines)
                if !text.isEmpty {
                    parts.append(text)
                }
            }
            return parts.joined(separator: " ")
        }

        let audioFile = try AVAudioFile(forReading: URL(fileURLWithPath: audioPath))
        if let lastSampleTime = try await analyzer.analyzeSequence(from: audioFile) {
            try await analyzer.finalizeAndFinish(through: lastSampleTime)
        } else {
            await analyzer.cancelAndFinishNow()
        }

        let transcript = try await resultTask.value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !transcript.isEmpty else {
            throw TranscriberError.emptyTranscript
        }
        return transcript
    }
}
