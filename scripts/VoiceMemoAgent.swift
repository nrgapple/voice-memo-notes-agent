import ApplicationServices
import AppKit
import CoreServices
import Darwin
import Foundation

private let voiceMemosBundleID = "com.apple.VoiceMemos"
private func iso8601(_ date: Date) -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter.string(from: date)
}
private enum AgentError: Error, CustomStringConvertible {
    case message(String)

    var description: String {
        switch self {
        case .message(let value): return value
        }
    }
}

private func argumentValue(_ name: String) -> String? {
    guard let index = CommandLine.arguments.firstIndex(of: name), index + 1 < CommandLine.arguments.count else {
        return nil
    }
    return CommandLine.arguments[index + 1]
}

private func hasArgument(_ name: String) -> Bool {
    CommandLine.arguments.contains(name)
}

private func printJSON(_ object: [String: Any], path: String? = nil) {
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else { return }
    let output = data + Data("\n".utf8)
    if let path {
        try? output.write(to: URL(fileURLWithPath: path), options: .atomic)
    } else {
        print(String(data: data, encoding: .utf8) ?? "{}")
    }
}

private func resultPath() -> String? {
    argumentValue("--result-file")
}

private func requiredValue(_ name: String) throws -> String {
    guard let value = argumentValue(name), !value.isEmpty else {
        throw AgentError.message("\(name) is required")
    }
    return value
}

private func intValue(_ name: String, default defaultValue: Int) throws -> Int {
    guard let raw = argumentValue(name) else { return defaultValue }
    guard let value = Int(raw), value >= 0 else {
        throw AgentError.message("\(name) must be a nonnegative integer")
    }
    return value
}

private func validPushoverCredential(_ value: String) -> Bool {
    value.range(of: #"^[A-Za-z0-9]{20,64}$"#, options: .regularExpression) != nil
}

private struct PushoverCredentials: Codable {
    let apiToken: String
    let userKey: String

    enum CodingKeys: String, CodingKey {
        case apiToken = "api_token"
        case userKey = "user_key"
    }
}

private func pushoverCredentialsPath() -> String {
    ProcessInfo.processInfo.environment["VOICE_MEMO_PUSHOVER_CREDENTIALS_FILE"] ??
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config/voice-memo-agent/pushover.json").path
}

private func loadPushoverCredentials() throws -> PushoverCredentials? {
    let path = pushoverCredentialsPath()
    guard FileManager.default.fileExists(atPath: path) else { return nil }
    let attributes = try FileManager.default.attributesOfItem(atPath: path)
    let permissions = (attributes[.posixPermissions] as? NSNumber)?.intValue ?? -1
    let ownerID = (attributes[.ownerAccountID] as? NSNumber)?.uint32Value
    guard permissions & 0o077 == 0, ownerID == getuid() else {
        throw AgentError.message("Pushover credentials must be owned by the current user with mode 0600")
    }
    let credentials = try JSONDecoder().decode(
        PushoverCredentials.self,
        from: Data(contentsOf: URL(fileURLWithPath: path))
    )
    guard validPushoverCredential(credentials.userKey), validPushoverCredential(credentials.apiToken) else {
        throw AgentError.message("Pushover credentials file contains invalid values")
    }
    return credentials
}

private func savePushoverCredentials(_ credentials: PushoverCredentials) throws {
    let destination = URL(fileURLWithPath: pushoverCredentialsPath())
    let directory = destination.deletingLastPathComponent()
    try FileManager.default.createDirectory(
        at: directory,
        withIntermediateDirectories: true,
        attributes: [.posixPermissions: 0o700]
    )
    try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
    try JSONEncoder().encode(credentials).write(to: destination, options: .atomic)
    guard chmod(destination.path, S_IRUSR | S_IWUSR) == 0 else {
        throw AgentError.message("could not secure the Pushover credentials file")
    }
}

private struct ImportNotificationPayload {
    let title: String
    let message: String
    let url: String?
}

private struct WorkflowImport: Codable {
    let memoID: Int64
    let title: String
    let affectedNotes: [String]
    let commitSHA: String
    let githubURL: String?
    let renameStatus: String

    enum CodingKeys: String, CodingKey {
        case memoID = "memo_id"
        case title
        case affectedNotes = "affected_notes"
        case commitSHA = "commit_sha"
        case githubURL = "github_url"
        case renameStatus = "rename_status"
    }
}

private struct WorkflowFailure: Codable {
    let memoID: Int64?
    let stage: String
    let message: String

    enum CodingKeys: String, CodingKey {
        case memoID = "memo_id"
        case stage
        case message
    }
}

private struct WorkflowReview: Codable {
    let memoID: Int64
    let title: String
    let affectedNotes: [String]
    let commitSHA: String
    let branch: String
    let reviewURL: String?

    enum CodingKeys: String, CodingKey {
        case memoID = "memo_id"
        case title
        case affectedNotes = "affected_notes"
        case commitSHA = "commit_sha"
        case branch
        case reviewURL = "review_url"
    }
}

private struct WorkflowMetrics: Codable {
    let codexCalls: Int
    let durationMS: Int?

    enum CodingKeys: String, CodingKey {
        case codexCalls = "codex_calls"
        case durationMS = "duration_ms"
    }
}

private struct WorkflowResult: Codable {
    let runID: String?
    let ok: Bool
    let noOp: Bool
    let imports: [WorkflowImport]
    let reviews: [WorkflowReview]?
    let actionableFailures: [WorkflowFailure]
    let ignoredCount: Int
    let metrics: WorkflowMetrics

    enum CodingKeys: String, CodingKey {
        case runID = "run_id"
        case ok
        case noOp = "no_op"
        case imports
        case reviews
        case actionableFailures = "actionable_failures"
        case ignoredCount = "ignored_count"
        case metrics
    }
}

private func processingStartedNotificationPayload(recordingCount: Int) -> ImportNotificationPayload {
    let subject = recordingCount == 1 ? "A new voice memo was" : "\(recordingCount) new voice memos were"
    return ImportNotificationPayload(
        title: "Voice memo found",
        message: "\(subject) found and processing has started.",
        url: nil
    )
}

private func reviewNotificationPayload(from review: WorkflowReview) -> ImportNotificationPayload {
    let notes = review.affectedNotes.joined(separator: ", ")
    let shortSHA = String(review.commitSHA.prefix(12))
    let body = "Memo \(review.memoID) is ready for review\nTitle: \(review.title)\nNotes: \(notes)\nCommit: \(shortSHA)"
    return ImportNotificationPayload(
        title: "Voice memo ready for review",
        message: String(body.prefix(900)),
        url: review.reviewURL
    )
}

private func importNotificationPayload(from imported: WorkflowImport) -> ImportNotificationPayload {
    let notes = imported.affectedNotes.joined(separator: ", ")
    let shortSHA = String(imported.commitSHA.prefix(12))
    let body = "Imported memo \(imported.memoID)\nTitle: \(imported.title)\nNotes: \(notes)\nCommit: \(shortSHA)"
    return ImportNotificationPayload(
        title: "Voice memo imported",
        message: String(body.prefix(900)),
        url: imported.githubURL
    )
}

private func failureNotificationPayload(from failure: WorkflowFailure) -> ImportNotificationPayload {
    let subject = failure.memoID.map { "Memo \($0)" } ?? "Voice Memo Agent"
    let isRename = failure.stage == "rename"
    let body = isRename
        ? "\(subject) needs attention\nStage: rename\nThe memo was imported, but its Voice Memos rename remains pending."
        : "\(subject) needs attention\nStage: \(failure.stage)\nThe import did not complete and remains queued for retry."
    return ImportNotificationPayload(
        title: isRename ? "Voice memo rename failed" : "Voice memo import failed",
        message: String(body.prefix(900)),
        url: nil
    )
}

private final class PushoverNotifier {
    func configured() throws -> Bool {
        try loadPushoverCredentials() != nil
    }

    func save(userKey: String, apiToken: String) throws {
        guard validPushoverCredential(userKey), validPushoverCredential(apiToken) else {
            throw AgentError.message("Pushover credentials must be 20-64 ASCII letters or numbers")
        }
        try savePushoverCredentials(PushoverCredentials(apiToken: apiToken, userKey: userKey))
    }

    func send(_ payload: ImportNotificationPayload) throws -> String {
        guard let credentials = try loadPushoverCredentials() else {
            throw AgentError.message("Pushover is not configured")
        }
        var items = [
            URLQueryItem(name: "token", value: credentials.apiToken),
            URLQueryItem(name: "user", value: credentials.userKey),
            URLQueryItem(name: "title", value: payload.title),
            URLQueryItem(name: "message", value: payload.message),
            URLQueryItem(name: "priority", value: "0"),
            URLQueryItem(name: "ttl", value: "86400"),
        ]
        if let url = payload.url {
            items.append(URLQueryItem(name: "url", value: url))
            items.append(URLQueryItem(name: "url_title", value: "Open commit"))
        }
        var components = URLComponents()
        components.queryItems = items
        var request = URLRequest(url: URL(string: "https://api.pushover.net/1/messages.json")!)
        request.httpMethod = "POST"
        request.timeoutInterval = 20
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        request.httpBody = components.percentEncodedQuery?.data(using: .utf8)

        let semaphore = DispatchSemaphore(value: 0)
        var responseData: Data?
        var responseStatus: Int?
        var responseError: Error?
        let session = URLSession(configuration: .ephemeral)
        let task = session.dataTask(with: request) { data, response, error in
            responseData = data
            responseStatus = (response as? HTTPURLResponse)?.statusCode
            responseError = error
            semaphore.signal()
        }
        task.resume()
        if semaphore.wait(timeout: .now() + 25) == .timedOut {
            task.cancel()
            throw AgentError.message("Pushover request timed out")
        }
        if let responseError { throw AgentError.message("Pushover request failed: \(responseError)") }
        guard responseStatus == 200, let responseData,
              let object = try? JSONSerialization.jsonObject(with: responseData) as? [String: Any],
              object["status"] as? Int == 1 else {
            let detail = responseData.flatMap { String(data: $0, encoding: .utf8) } ?? "no response"
            throw AgentError.message("Pushover rejected the request: \(String(detail.prefix(500)))")
        }
        return object["request"] as? String ?? "accepted"
    }
}

private final class AgentLogger {
    private let path: String
    private let lock = NSLock()

    init(path: String) {
        self.path = path
        let directory = URL(fileURLWithPath: path).deletingLastPathComponent().path
        try? FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true)
    }

    func write(_ event: String, fields: [String: Any] = [:]) {
        lock.lock()
        defer { lock.unlock() }
        rotateIfNeeded()
        var payload = fields
        payload["at"] = iso8601(Date())
        payload["event"] = event
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]) else { return }
        if !FileManager.default.fileExists(atPath: path) {
            FileManager.default.createFile(atPath: path, contents: nil)
        }
        guard let handle = FileHandle(forWritingAtPath: path) else { return }
        defer { try? handle.close() }
        do {
            try handle.seekToEnd()
            try handle.write(contentsOf: data + Data("\n".utf8))
        } catch {}
    }

    private func rotateIfNeeded() {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: path),
              let size = attributes[.size] as? NSNumber,
              size.intValue > 5 * 1024 * 1024 else { return }
        let previous = path + ".1"
        try? FileManager.default.removeItem(atPath: previous)
        try? FileManager.default.moveItem(atPath: path, toPath: previous)
    }
}

private final class DemoLogger {
    private let path: String
    private let minimumInterval: TimeInterval
    private let queue = DispatchQueue(label: "com.nrgapple.VoiceMemoAgent.demo-log")
    private var lastWriteAt: Date?

    init(path: String, minimumIntervalMilliseconds: Int) {
        self.path = path
        self.minimumInterval = TimeInterval(minimumIntervalMilliseconds) / 1000
        let directory = URL(fileURLWithPath: path).deletingLastPathComponent().path
        try? FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: path) {
            FileManager.default.createFile(atPath: path, contents: nil)
        }
    }

    func write(_ message: String) {
        queue.async {
            if let lastWriteAt = self.lastWriteAt {
                let remaining = self.minimumInterval - Date().timeIntervalSince(lastWriteAt)
                if remaining > 0 {
                    Thread.sleep(forTimeInterval: remaining)
                }
            }
            self.rotateIfNeeded()
            let line = "\(message)\n"
            if !FileManager.default.fileExists(atPath: self.path) {
                FileManager.default.createFile(atPath: self.path, contents: nil)
            }
            guard let handle = FileHandle(forWritingAtPath: self.path) else { return }
            defer { try? handle.close() }
            do {
                try handle.seekToEnd()
                try handle.write(contentsOf: Data(line.utf8))
                self.lastWriteAt = Date()
            } catch {}
        }
    }

    func flush() {
        queue.sync {}
    }

    private func rotateIfNeeded() {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: path),
              let size = attributes[.size] as? NSNumber,
              size.intValue > 5 * 1024 * 1024 else { return }
        let previous = path + ".1"
        try? FileManager.default.removeItem(atPath: previous)
        try? FileManager.default.moveItem(atPath: path, toPath: previous)
    }
}

private final class DemoProgressReader {
    private let logger: DemoLogger
    private let lock = NSLock()
    private var buffer = ""

    init(logger: DemoLogger) {
        self.logger = logger
    }

    func consume(_ data: Data) {
        guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
        lock.lock()
        defer { lock.unlock() }
        buffer += text
        while let newline = buffer.firstIndex(of: "\n") {
            let line = String(buffer[..<newline])
            buffer.removeSubrange(...newline)
            handle(line)
        }
    }

    func finish() {
        lock.lock()
        defer { lock.unlock() }
        if !buffer.isEmpty {
            handle(buffer)
            buffer = ""
        }
    }

    private func handle(_ line: String) {
        guard line.hasPrefix("voice-memo-demo:") else { return }
        switch String(line.dropFirst("voice-memo-demo:".count)) {
        case "listening":
            logger.write("📝 Transcribing on this Mac now. The recording stays local while I turn speech into text.")
        case "qualified":
            logger.write("💡 Found the “work note” cue. That opt-in tells me this memo is allowed into your work notes.")
        case "organizing":
            logger.write("🔎 Comparing the memo with your existing notes and links to find the best destination.")
        case "drafting":
            logger.write("🧠 Relevant context found. I’m writing a concise update instead of copying the raw transcript.")
        case "validated":
            logger.write("🛡️ Safety checks passed: additive changes only, valid links, and no overwritten note content.")
        case "review-ready":
            logger.write("✨ Saved on a private review branch. It’s ready to inspect before reaching your main notes.")
        case "imported":
            logger.write("✅ Update committed and pushed. Your notes are current; I’ll rename the recording separately.")
        case "ignored":
            logger.write("🔒 No work cue found. I kept the transcript local and left your notes unchanged.")
        default:
            break
        }
    }
}

private func sendFailureNotification(
    _ failure: WorkflowFailure,
    runID: String,
    logger: AgentLogger
) {
    do {
        let notifier = PushoverNotifier()
        if try notifier.configured() {
            let notificationStarted = Date()
            let requestID = try notifier.send(failureNotificationPayload(from: failure))
            logger.write("notification-sent", fields: [
                "provider": "pushover", "request_id": requestID,
                "memo_id": failure.memoID ?? NSNull(), "stage": failure.stage,
                "run_id": runID, "kind": "failure",
                "duration_ms": Int(Date().timeIntervalSince(notificationStarted) * 1000),
            ])
        } else {
            logger.write("notification-skipped", fields: [
                "provider": "pushover", "reason": "not-configured",
                "kind": "failure", "stage": failure.stage, "run_id": runID,
            ])
        }
    } catch {
        logger.write("notification-failed", fields: [
            "provider": "pushover", "memo_id": failure.memoID ?? NSNull(),
            "stage": failure.stage, "run_id": runID,
            "kind": "failure", "error": String(describing: error),
        ])
    }
}

private func sendProcessingStartedNotification(
    recordingCount: Int,
    runID: String,
    logger: AgentLogger
) {
    do {
        let notifier = PushoverNotifier()
        if try notifier.configured() {
            let notificationStarted = Date()
            let requestID = try notifier.send(
                processingStartedNotificationPayload(recordingCount: recordingCount)
            )
            logger.write("notification-sent", fields: [
                "provider": "pushover", "request_id": requestID,
                "recording_count": recordingCount, "run_id": runID,
                "kind": "processing-started",
                "duration_ms": Int(Date().timeIntervalSince(notificationStarted) * 1000),
            ])
        } else {
            logger.write("notification-skipped", fields: [
                "provider": "pushover", "reason": "not-configured",
                "recording_count": recordingCount, "run_id": runID,
                "kind": "processing-started",
            ])
        }
    } catch {
        logger.write("notification-failed", fields: [
            "provider": "pushover", "recording_count": recordingCount,
            "run_id": runID, "kind": "processing-started",
            "error": String(describing: error),
        ])
    }
}

private struct SyncConfiguration {
    let codexPath: String
    let ghPath: String
    let nodePath: String
    let pythonPath: String
    let syncScript: String
    let repo: String
    let stateDirectory: String
    let logPath: String
    let codexLogPath: String
    let demoLogPath: String
    let demoLogIntervalMilliseconds: Int
    let syncTimeoutSeconds: Int
}

private struct SyncResult {
    let exitCode: Int32
    let message: String
    let workflow: WorkflowResult?
}

private final class SyncRunner {
    private let configuration: SyncConfiguration
    private let logger: AgentLogger
    private let demoLogger: DemoLogger?

    init(configuration: SyncConfiguration, logger: AgentLogger, demoLogger: DemoLogger? = nil) {
        self.configuration = configuration
        self.logger = logger
        self.demoLogger = demoLogger
    }

    func run(
        reason: String,
        recordingFiles: [String] = [],
        detectedAt: Date? = nil,
        runID: String = UUID().uuidString.lowercased()
    ) -> SyncResult {
        let isRecordingRun = detectedAt != nil && !recordingFiles.isEmpty
        let resultPath = configuration.stateDirectory + "/agent-last-message.txt"
        try? FileManager.default.removeItem(atPath: resultPath)
        logger.write("sync-started", fields: [
            "reason": reason, "run_id": runID, "trigger_files": recordingFiles.count,
            "detected_at": detectedAt.map(iso8601) ?? NSNull(),
        ])
        let process = Process()
        process.executableURL = URL(fileURLWithPath: configuration.pythonPath)
        process.arguments = [
            configuration.syncScript,
            "--repo", configuration.repo,
            "--codex-path", configuration.codexPath,
            "--node-path", configuration.nodePath,
            "--result-file", resultPath,
            "--run-id", runID,
        ]
        if let detectedAt {
            process.arguments?.append(contentsOf: ["--detected-at", iso8601(detectedAt)])
        }
        for file in recordingFiles {
            process.arguments?.append(contentsOf: ["--recording-file", file])
        }
        if isRecordingRun {
            process.arguments?.append("--demo-progress")
        }
        var environment = ProcessInfo.processInfo.environment
        environment["HOME"] = FileManager.default.homeDirectoryForCurrentUser.path
        environment["CODEX_HOME"] = environment["CODEX_HOME"] ?? environment["HOME"]! + "/.codex"
        environment["PATH"] = [
            URL(fileURLWithPath: configuration.codexPath).deletingLastPathComponent().path,
            URL(fileURLWithPath: configuration.ghPath).deletingLastPathComponent().path,
            URL(fileURLWithPath: configuration.nodePath).deletingLastPathComponent().path,
            URL(fileURLWithPath: configuration.pythonPath).deletingLastPathComponent().path,
            "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
        ].joined(separator: ":")
        process.environment = environment

        if !FileManager.default.fileExists(atPath: configuration.codexLogPath) {
            FileManager.default.createFile(atPath: configuration.codexLogPath, contents: nil)
        }
        let outputHandle = FileHandle(forWritingAtPath: configuration.codexLogPath)
        _ = try? outputHandle?.seekToEnd()
        var progressPipe: Pipe?
        var progressGroup: DispatchGroup?
        if isRecordingRun, let demoLogger {
            let pipe = Pipe()
            let reader = DemoProgressReader(logger: demoLogger)
            let group = DispatchGroup()
            group.enter()
            DispatchQueue.global(qos: .utility).async {
                while true {
                    let data = pipe.fileHandleForReading.availableData
                    if data.isEmpty { break }
                    reader.consume(data)
                }
                reader.finish()
                group.leave()
            }
            progressPipe = pipe
            progressGroup = group
            process.standardOutput = pipe
        } else {
            process.standardOutput = outputHandle
        }
        process.standardError = outputHandle

        var timedOut = false
        do {
            try process.run()
            try? progressPipe?.fileHandleForWriting.close()
            let deadline = Date().addingTimeInterval(TimeInterval(configuration.syncTimeoutSeconds))
            if !recordingFiles.isEmpty {
                sendProcessingStartedNotification(
                    recordingCount: recordingFiles.count,
                    runID: runID,
                    logger: logger
                )
            }
            while process.isRunning && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.2)
            }
            if process.isRunning {
                timedOut = true
                process.terminate()
                let terminationDeadline = Date().addingTimeInterval(5)
                while process.isRunning && Date() < terminationDeadline {
                    Thread.sleep(forTimeInterval: 0.1)
                }
                if process.isRunning {
                    kill(process.processIdentifier, SIGKILL)
                }
            }
            process.waitUntilExit()
            progressGroup?.wait()
        } catch {
            try? progressPipe?.fileHandleForWriting.close()
            progressGroup?.wait()
            try? outputHandle?.close()
            let message = "could not launch deterministic sync: \(error)"
            logger.write("sync-failed", fields: ["reason": reason, "run_id": runID, "error": message])
            sendFailureNotification(
                WorkflowFailure(memoID: nil, stage: "runtime", message: message),
                runID: runID,
                logger: logger
            )
            if isRecordingRun {
                demoLogger?.write("⚠️ The import needs attention. I preserved your existing notes and recorded a privacy-safe failure for review.")
            }
            return SyncResult(exitCode: 127, message: message, workflow: nil)
        }
        try? outputHandle?.close()

        var message = (try? String(contentsOfFile: resultPath, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if timedOut {
            message = "deterministic sync timed out after \(configuration.syncTimeoutSeconds) seconds"
        }
        let workflow = message.data(using: .utf8).flatMap { try? JSONDecoder().decode(WorkflowResult.self, from: $0) }
        let effectiveExitCode: Int32 = timedOut ? 124 : (workflow == nil && process.terminationStatus == 0 ? 65 : process.terminationStatus)
        if let workflow {
            let notifier = PushoverNotifier()
            for review in workflow.reviews ?? [] {
                do {
                    if try notifier.configured() {
                        let notificationStarted = Date()
                        let requestID = try notifier.send(reviewNotificationPayload(from: review))
                        logger.write("notification-sent", fields: [
                            "provider": "pushover", "request_id": requestID, "memo_id": review.memoID,
                            "run_id": runID,
                            "kind": "review",
                            "duration_ms": Int(Date().timeIntervalSince(notificationStarted) * 1000),
                        ])
                    } else {
                        logger.write("notification-skipped", fields: [
                            "provider": "pushover", "reason": "not-configured", "kind": "review", "run_id": runID,
                        ])
                    }
                } catch {
                    logger.write("notification-failed", fields: [
                        "provider": "pushover", "memo_id": review.memoID, "run_id": runID,
                        "kind": "review", "error": String(describing: error),
                    ])
                }
            }
            for imported in workflow.imports {
                do {
                    if try notifier.configured() {
                        let notificationStarted = Date()
                        let requestID = try notifier.send(importNotificationPayload(from: imported))
                        logger.write("notification-sent", fields: [
                            "provider": "pushover", "request_id": requestID, "memo_id": imported.memoID,
                            "run_id": runID,
                            "kind": "import",
                            "duration_ms": Int(Date().timeIntervalSince(notificationStarted) * 1000),
                        ])
                    } else {
                        logger.write("notification-skipped", fields: [
                            "provider": "pushover", "reason": "not-configured", "kind": "import", "run_id": runID,
                        ])
                    }
                } catch {
                    logger.write("notification-failed", fields: [
                        "provider": "pushover", "memo_id": imported.memoID, "run_id": runID,
                        "kind": "import", "error": String(describing: error),
                    ])
                }
            }
            for failure in workflow.actionableFailures {
                sendFailureNotification(failure, runID: runID, logger: logger)
            }
        } else {
            sendFailureNotification(
                WorkflowFailure(memoID: nil, stage: "runtime", message: "sync returned no structured result"),
                runID: runID,
                logger: logger
            )
        }
        let event = effectiveExitCode == 0 ? "sync-completed" : "sync-failed"
        logger.write(event, fields: [
            "reason": reason,
            "run_id": runID,
            "exit_code": effectiveExitCode,
            "codex_calls": workflow?.metrics.codexCalls ?? NSNull(),
            "duration_ms": workflow?.metrics.durationMS ?? NSNull(),
            "imports": workflow?.imports.count ?? NSNull(),
            "reviews": workflow?.reviews?.count ?? NSNull(),
            "actionable_failures": workflow?.actionableFailures.count ?? NSNull(),
        ])
        if isRecordingRun {
            if effectiveExitCode != 0 || !(workflow?.actionableFailures.isEmpty ?? true) {
                demoLogger?.write("⚠️ The import needs attention. I preserved your existing notes and recorded a privacy-safe failure for review.")
            } else if (workflow?.noOp ?? true) && (workflow?.ignoredCount ?? 0) == 0 {
                demoLogger?.write("👀 The recording is visible, but it is not ready to process yet. I’ll pick it up during reconciliation.")
            }
        }
        return SyncResult(exitCode: effectiveExitCode, message: message, workflow: workflow)
    }
}

private final class SyncCoordinator {
    private let runner: SyncRunner
    private let logger: AgentLogger
    private let debounceSeconds: Int
    private let queue = DispatchQueue(label: "com.nrgapple.VoiceMemoAgent.coordinator")
    private let worker = DispatchQueue(label: "com.nrgapple.VoiceMemoAgent.worker")
    private var pending: DispatchWorkItem?
    private var running = false
    private var rerunRequested = false
    private var pendingReasons: Set<String> = []
    private var pendingRecordingFiles: Set<String> = []
    private var earliestDetection: Date?
    private var pendingRunID: String?

    init(runner: SyncRunner, logger: AgentLogger, debounceSeconds: Int) {
        self.runner = runner
        self.logger = logger
        self.debounceSeconds = debounceSeconds
    }

    func schedule(
        reason: String,
        recordingFiles: [String] = [],
        detectedAt: Date? = nil,
        delay: Int? = nil,
        runID: String? = nil
    ) {
        let requestedRunID = runID ?? UUID().uuidString.lowercased()
        queue.async {
            if self.pendingRunID == nil { self.pendingRunID = requestedRunID }
            let scheduledRunID = self.pendingRunID!
            self.pendingReasons.insert(reason)
            self.pendingRecordingFiles.formUnion(recordingFiles)
            if let detectedAt, self.earliestDetection == nil || detectedAt < self.earliestDetection! {
                self.earliestDetection = detectedAt
            }
            if self.running {
                self.rerunRequested = true
                self.logger.write("sync-queued", fields: [
                    "reason": reason, "running": true, "run_id": scheduledRunID,
                ])
                return
            }
            self.pending?.cancel()
            let item = DispatchWorkItem { [weak self] in self?.startRun() }
            self.pending = item
            let wait = DispatchTime.now() + .seconds(delay ?? self.debounceSeconds)
            self.queue.asyncAfter(deadline: wait, execute: item)
            self.logger.write("sync-scheduled", fields: [
                "reason": reason,
                "delay_seconds": delay ?? self.debounceSeconds,
                "run_id": scheduledRunID,
            ])
        }
    }

    private func startRun() {
        guard !running else {
            rerunRequested = true
            return
        }
        pending = nil
        running = true
        let reason = pendingReasons.sorted().joined(separator: ",")
        let recordingFiles = pendingRecordingFiles.sorted()
        let detectedAt = earliestDetection
        let runID = pendingRunID ?? UUID().uuidString.lowercased()
        pendingReasons.removeAll()
        pendingRecordingFiles.removeAll()
        earliestDetection = nil
        pendingRunID = nil
        worker.async {
            let result = self.runner.run(
                reason: reason,
                recordingFiles: recordingFiles,
                detectedAt: detectedAt,
                runID: runID
            )
            self.queue.async {
                self.running = false
                let hasPendingRename = result.workflow?.imports.contains { $0.renameStatus == "pending" } ?? false
                if hasPendingRename {
                    self.schedule(reason: "post-import-rename", delay: 2)
                }
                if self.rerunRequested || !self.pendingReasons.isEmpty {
                    self.rerunRequested = false
                    self.schedule(reason: "queued-during-sync", delay: 5)
                }
            }
        }
    }
}

private struct EventState: Codable {
    var lastEventID: UInt64
}

private func shouldTriggerMemo(path: String, flags: FSEventStreamEventFlags) -> Bool {
    let created = flags & FSEventStreamEventFlags(kFSEventStreamEventFlagItemCreated) != 0
    let file = flags & FSEventStreamEventFlags(kFSEventStreamEventFlagItemIsFile) != 0
    return created && file && URL(fileURLWithPath: path).pathExtension.caseInsensitiveCompare("m4a") == .orderedSame
}

private final class MemoWatcher {
    private let recordingsDirectory: String
    private let statePath: String
    private let logger: AgentLogger
    private let demoLogger: DemoLogger
    private let coordinator: SyncCoordinator
    private let queue = DispatchQueue(label: "com.nrgapple.VoiceMemoAgent.fsevents")
    private var stream: FSEventStreamRef?
    private var knownRecordings: Set<String> = []

    init(
        recordingsDirectory: String,
        stateDirectory: String,
        logger: AgentLogger,
        demoLogger: DemoLogger,
        coordinator: SyncCoordinator
    ) {
        self.recordingsDirectory = recordingsDirectory
        self.statePath = stateDirectory + "/agent-event-state.json"
        self.logger = logger
        self.demoLogger = demoLogger
        self.coordinator = coordinator
    }

    func start() throws {
        do {
            let names = try FileManager.default.contentsOfDirectory(atPath: recordingsDirectory)
            knownRecordings = Set(names.compactMap { name in
                let path = URL(fileURLWithPath: recordingsDirectory).appendingPathComponent(name).standardizedFileURL.path
                return URL(fileURLWithPath: path).pathExtension.caseInsensitiveCompare("m4a") == .orderedSame ? path : nil
            })
        } catch {
            throw AgentError.message("cannot read Voice Memos directory; grant Full Disk Access to Voice Memo Agent")
        }

        let storedID = loadEventID()
        let since = storedID == 0 ? FSEventsGetCurrentEventId() : FSEventStreamEventId(storedID)
        var context = FSEventStreamContext(
            version: 0,
            info: Unmanaged.passUnretained(self).toOpaque(),
            retain: nil,
            release: nil,
            copyDescription: nil
        )
        let callback: FSEventStreamCallback = { _, info, count, rawPaths, flags, ids in
            guard let info else { return }
            let watcher = Unmanaged<MemoWatcher>.fromOpaque(info).takeUnretainedValue()
            let paths = unsafeBitCast(rawPaths, to: NSArray.self) as? [String] ?? []
            watcher.handle(paths: paths, count: count, flags: flags, ids: ids)
        }
        let createFlags = FSEventStreamCreateFlags(
            kFSEventStreamCreateFlagFileEvents |
            kFSEventStreamCreateFlagNoDefer |
            kFSEventStreamCreateFlagUseCFTypes |
            kFSEventStreamCreateFlagWatchRoot
        )
        guard let stream = FSEventStreamCreate(
            kCFAllocatorDefault,
            callback,
            &context,
            [recordingsDirectory] as CFArray,
            since,
            1.0,
            createFlags
        ) else {
            throw AgentError.message("could not create the Voice Memos FSEvents stream")
        }
        self.stream = stream
        FSEventStreamSetDispatchQueue(stream, queue)
        guard FSEventStreamStart(stream) else {
            FSEventStreamInvalidate(stream)
            FSEventStreamRelease(stream)
            self.stream = nil
            throw AgentError.message("could not start the Voice Memos FSEvents stream")
        }
        logger.write("watcher-started", fields: ["path": recordingsDirectory, "since_event_id": since])
    }

    private func handle(
        paths: [String],
        count: Int,
        flags: UnsafePointer<FSEventStreamEventFlags>,
        ids: UnsafePointer<FSEventStreamEventId>
    ) {
        var newestID: UInt64 = 0
        var created: [String] = []
        var requiresScan = false
        for index in 0..<min(count, paths.count) {
            let eventFlags = flags[index]
            newestID = max(newestID, UInt64(ids[index]))
            if eventFlags & FSEventStreamEventFlags(kFSEventStreamEventFlagMustScanSubDirs) != 0 ||
                eventFlags & FSEventStreamEventFlags(kFSEventStreamEventFlagUserDropped) != 0 ||
                eventFlags & FSEventStreamEventFlags(kFSEventStreamEventFlagKernelDropped) != 0 ||
                eventFlags & FSEventStreamEventFlags(kFSEventStreamEventFlagRootChanged) != 0 {
                requiresScan = true
            }
            let path = URL(fileURLWithPath: paths[index]).standardizedFileURL.path
            let removed = eventFlags & FSEventStreamEventFlags(kFSEventStreamEventFlagItemRemoved) != 0
            if removed {
                knownRecordings.remove(path)
            } else if shouldTriggerMemo(path: path, flags: eventFlags), !knownRecordings.contains(path) {
                knownRecordings.insert(path)
                created.append(path)
            }
        }
        if newestID > 0 { saveEventID(newestID) }
        if requiresScan {
            logger.write("fsevents-rescan-required", fields: ["last_event_id": newestID])
            coordinator.schedule(reason: "fsevents-rescan")
        }
        if !created.isEmpty {
            let detectedAt = Date()
            let runID = UUID().uuidString.lowercased()
            logger.write("recording-created", fields: [
                "files": created.map { URL(fileURLWithPath: $0).lastPathComponent },
                "detected_at": iso8601(detectedAt),
                "run_id": runID,
            ])
            demoLogger.write("🎙️ New memo detected. I’ll process the audio locally before touching your notes.")
            coordinator.schedule(
                reason: "recording-created",
                recordingFiles: created,
                detectedAt: detectedAt,
                runID: runID
            )
        }
    }

    private func loadEventID() -> UInt64 {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: statePath)),
              let state = try? JSONDecoder().decode(EventState.self, from: data) else { return 0 }
        return state.lastEventID
    }

    private func saveEventID(_ value: UInt64) {
        guard let data = try? JSONEncoder().encode(EventState(lastEventID: value)) else { return }
        try? data.write(to: URL(fileURLWithPath: statePath), options: .atomic)
    }
}

private final class SessionActivityWatcher {
    private var observers: [NSObjectProtocol] = []

    init(logger: AgentLogger, coordinator: SyncCoordinator) {
        let center = NSWorkspace.shared.notificationCenter
        let notifications: [Notification.Name] = [
            NSWorkspace.didWakeNotification,
            NSWorkspace.screensDidWakeNotification,
            NSWorkspace.sessionDidBecomeActiveNotification,
        ]
        observers = notifications.map { name in
            center.addObserver(forName: name, object: nil, queue: nil) { _ in
                logger.write("session-active", fields: ["notification": name.rawValue])
                coordinator.schedule(reason: "session-active", delay: 5)
            }
        }
    }

    deinit {
        let center = NSWorkspace.shared.notificationCenter
        observers.forEach { center.removeObserver($0) }
    }
}

private struct RenameOptions {
    let memoID: Int64
    let currentTitle: String
    let newTitle: String
    let recordedAt: Date?
    let duration: Double?
    let resultFile: String?
}

private func parseISO8601Date(_ value: String?) -> Date? {
    guard let value else { return nil }
    let fractional = ISO8601DateFormatter()
    fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return fractional.date(from: value) ?? ISO8601DateFormatter().date(from: value)
}

private func parseRenameOptions() throws -> RenameOptions {
    guard let idText = argumentValue("--memo-id"), let memoID = Int64(idText),
          let currentTitle = argumentValue("--current-title"),
          let newTitle = argumentValue("--new-title") else {
        throw AgentError.message("--memo-id, --current-title, and --new-title are required")
    }
    guard !currentTitle.isEmpty, !newTitle.isEmpty else {
        throw AgentError.message("titles must not be empty")
    }
    let recordedAt = parseISO8601Date(argumentValue("--recorded-at"))
    let duration = argumentValue("--duration").flatMap(Double.init)
    return RenameOptions(
        memoID: memoID,
        currentTitle: currentTitle,
        newTitle: newTitle,
        recordedAt: recordedAt,
        duration: duration,
        resultFile: resultPath()
    )
}

private func axValue(_ element: AXUIElement, _ attribute: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success else { return nil }
    return value
}

private func axString(_ element: AXUIElement, _ attribute: String) -> String? {
    axValue(element, attribute) as? String
}

private func descendants(of root: AXUIElement, limit: Int = 5000) -> [AXUIElement] {
    var result: [AXUIElement] = []
    var queue: [AXUIElement] = [root]
    var cursor = 0
    while cursor < queue.count && result.count < limit {
        let element = queue[cursor]
        cursor += 1
        result.append(element)
        if let children = axValue(element, kAXChildrenAttribute) as? [AXUIElement] {
            queue.append(contentsOf: children)
        }
    }
    return result
}

private func waitFor<T>(seconds: TimeInterval, operation: () -> T?) -> T? {
    let deadline = Date().addingTimeInterval(seconds)
    repeat {
        if let result = operation() { return result }
        RunLoop.current.run(until: Date().addingTimeInterval(0.2))
    } while Date() < deadline
    return nil
}

private func recordingRows(in app: AXUIElement, title: String) -> [AXUIElement] {
    let prefix = title + ","
    return descendants(of: app).filter { element in
        axString(element, kAXRoleAttribute) == kAXButtonRole &&
            (axString(element, kAXDescriptionAttribute).map { $0 == title || $0.hasPrefix(prefix) } ?? false)
    }
}

private func renameRowScore(description: String, recordedAt: Date?, duration: Double?) -> Int {
    let folded = description.lowercased()
    var score = 0
    if let recordedAt {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = .current
        for (format, points) in [("MMMM d", 5), ("MMM d", 4), ("h:mm a", 5), ("M/d/yyyy", 6)] {
            formatter.dateFormat = format
            if folded.contains(formatter.string(from: recordedAt).lowercased()) { score += points }
        }
    }
    if let duration {
        let seconds = max(0, Int(duration.rounded()))
        let clock = String(format: "%d:%02d", seconds / 60, seconds % 60)
        if folded.contains(clock) { score += 6 }
        let unitPatterns = ["\(seconds) second", "\(seconds) sec"]
        if unitPatterns.contains(where: { folded.contains($0) }) { score += 5 }
    }
    return score
}

private func selectRecordingRow(_ rows: [AXUIElement], options: RenameOptions) -> (AXUIElement, String)? {
    if rows.count == 1, let row = rows.first { return (row, "unique-title") }
    let ranked = rows.map { row in
        (row, renameRowScore(
            description: axString(row, kAXDescriptionAttribute) ?? "",
            recordedAt: options.recordedAt,
            duration: options.duration
        ))
    }.sorted { $0.1 > $1.1 }
    guard let best = ranked.first, best.1 > 0,
          ranked.count == 1 || best.1 > ranked[1].1 else { return nil }
    return (best.0, "date-duration")
}

private func searchField(in app: AXUIElement) -> AXUIElement? {
    descendants(of: app).first { element in
        axString(element, kAXRoleAttribute) == kAXTextFieldRole &&
            axString(element, kAXDescriptionAttribute) == "Titles, Transcripts"
    }
}

private func titleField(in app: AXUIElement, currentTitle: String) -> AXUIElement? {
    descendants(of: app).first { element in
        axString(element, kAXRoleAttribute) == kAXTextFieldRole &&
            axString(element, kAXDescriptionAttribute) != "Titles, Transcripts" &&
            axString(element, kAXValueAttribute) == currentTitle
    }
}

private func postReturn() {
    let keyCode: CGKeyCode = 36
    CGEvent(keyboardEventSource: nil, virtualKey: keyCode, keyDown: true)?.post(tap: .cghidEventTap)
    CGEvent(keyboardEventSource: nil, virtualKey: keyCode, keyDown: false)?.post(tap: .cghidEventTap)
}

private func runRename() throws {
    let options = try parseRenameOptions()
    if options.currentTitle == options.newTitle {
        printJSON(["changed": false, "memo_id": options.memoID, "ok": true, "title": options.newTitle], path: options.resultFile)
        return
    }
    guard AXIsProcessTrusted() else {
        throw AgentError.message("Accessibility access is required for Voice Memo Agent")
    }
    if NSRunningApplication.runningApplications(withBundleIdentifier: voiceMemosBundleID).isEmpty {
        guard let appURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: voiceMemosBundleID) else {
            throw AgentError.message("Voice Memos application was not found")
        }
        NSWorkspace.shared.openApplication(at: appURL, configuration: NSWorkspace.OpenConfiguration())
    }
    guard let running = waitFor(seconds: 10, operation: {
        NSRunningApplication.runningApplications(withBundleIdentifier: voiceMemosBundleID).first
    }) else {
        throw AgentError.message("Voice Memos did not launch")
    }
    running.activate()
    let app = AXUIElementCreateApplication(running.processIdentifier)
    var usedSearch = false
    var rows = waitFor(seconds: 5) { () -> [AXUIElement]? in
        let matches = recordingRows(in: app, title: options.currentTitle)
        return matches.isEmpty ? nil : matches
    } ?? []
    if rows.isEmpty, let field = searchField(in: app) {
        guard AXUIElementSetAttributeValue(field, kAXValueAttribute as CFString, options.currentTitle as CFTypeRef) == .success else {
            throw AgentError.message("could not search Voice Memos for the current title")
        }
        usedSearch = true
        rows = waitFor(seconds: 5) { () -> [AXUIElement]? in
            let matches = recordingRows(in: app, title: options.currentTitle)
            return matches.isEmpty ? nil : matches
        } ?? []
    }
    guard let (row, selectionStrategy) = selectRecordingRow(rows, options: options) else {
        if usedSearch, let field = searchField(in: app) {
            AXUIElementSetAttributeValue(field, kAXValueAttribute as CFString, "" as CFTypeRef)
        }
        throw AgentError.message("expected one matching recording row, found \(rows.count)")
    }
    guard AXUIElementPerformAction(row, kAXPressAction as CFString) == .success else {
        throw AgentError.message("could not select the recording")
    }
    guard let field = waitFor(seconds: 5, operation: { titleField(in: app, currentTitle: options.currentTitle) }) else {
        throw AgentError.message("selected recording did not expose an editable title")
    }
    AXUIElementSetAttributeValue(field, kAXFocusedAttribute as CFString, kCFBooleanTrue)
    guard AXUIElementSetAttributeValue(field, kAXValueAttribute as CFString, options.newTitle as CFTypeRef) == .success else {
        throw AgentError.message("Voice Memos rejected the new title")
    }
    postReturn()
    guard waitFor(seconds: 10, operation: {
        titleField(in: app, currentTitle: options.newTitle) != nil ? true : nil
    }) != nil else {
        throw AgentError.message("Voice Memos did not persist the new title")
    }
    if usedSearch, let search = searchField(in: app) {
        AXUIElementSetAttributeValue(search, kAXValueAttribute as CFString, "" as CFTypeRef)
    }
    printJSON([
        "changed": true,
        "memo_id": options.memoID,
        "ok": true,
        "original_title": options.currentTitle,
        "selection_strategy": selectionStrategy,
        "title": options.newTitle,
    ], path: options.resultFile)
}

private func syncConfiguration() throws -> SyncConfiguration {
    let repo = try requiredValue("--repo")
    let stateDirectory = argumentValue("--state-dir") ?? repo + "/.voice-memo-automation"
    return SyncConfiguration(
        codexPath: try requiredValue("--codex-path"),
        ghPath: try requiredValue("--gh-path"),
        nodePath: try requiredValue("--node-path"),
        pythonPath: try requiredValue("--python-path"),
        syncScript: try requiredValue("--sync-script"),
        repo: repo,
        stateDirectory: stateDirectory,
        logPath: argumentValue("--log-path") ?? stateDirectory + "/agent.log",
        codexLogPath: argumentValue("--codex-log-path") ?? stateDirectory + "/agent-codex.log",
        demoLogPath: argumentValue("--demo-log-path") ?? stateDirectory + "/agent-demo.log",
        demoLogIntervalMilliseconds: try intValue("--demo-log-interval-ms", default: 1500),
        syncTimeoutSeconds: try intValue("--sync-timeout-seconds", default: 600)
    )
}

private func runWatch() throws -> Never {
    let configuration = try syncConfiguration()
    let recordings = try requiredValue("--recordings-dir")
    let debounce = try intValue("--debounce-seconds", default: 2)
    let reconcile = try intValue("--reconcile-seconds", default: 21_600)
    try FileManager.default.createDirectory(atPath: configuration.stateDirectory, withIntermediateDirectories: true)
    let logger = AgentLogger(path: configuration.logPath)
    let demoLogger = DemoLogger(
        path: configuration.demoLogPath,
        minimumIntervalMilliseconds: configuration.demoLogIntervalMilliseconds
    )
    let runner = SyncRunner(configuration: configuration, logger: logger, demoLogger: demoLogger)
    let coordinator = SyncCoordinator(runner: runner, logger: logger, debounceSeconds: debounce)
    let watcher = MemoWatcher(
        recordingsDirectory: recordings,
        stateDirectory: configuration.stateDirectory,
        logger: logger,
        demoLogger: demoLogger,
        coordinator: coordinator
    )
    let sessionActivityWatcher = SessionActivityWatcher(logger: logger, coordinator: coordinator)
    var watcherFailureReported = false
    while true {
        do {
            try watcher.start()
            break
        } catch {
            logger.write("watcher-waiting-for-access", fields: ["error": String(describing: error)])
            if !watcherFailureReported {
                sendFailureNotification(
                    WorkflowFailure(memoID: nil, stage: "watcher", message: String(describing: error)),
                    runID: UUID().uuidString.lowercased(),
                    logger: logger
                )
                watcherFailureReported = true
            }
            Thread.sleep(forTimeInterval: 60)
        }
    }
    coordinator.schedule(reason: "watcher-startup", delay: 5)
    var reconciliationTimer: DispatchSourceTimer?
    if reconcile > 0 {
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .utility))
        timer.schedule(deadline: .now() + .seconds(reconcile), repeating: .seconds(reconcile))
        timer.setEventHandler { coordinator.schedule(reason: "periodic-reconciliation", delay: 0) }
        timer.resume()
        reconciliationTimer = timer
    }
    withExtendedLifetime((reconciliationTimer, sessionActivityWatcher)) { dispatchMain() }
}

private func runSyncOnce() throws {
    let configuration = try syncConfiguration()
    try FileManager.default.createDirectory(atPath: configuration.stateDirectory, withIntermediateDirectories: true)
    let logger = AgentLogger(path: configuration.logPath)
    let result = SyncRunner(configuration: configuration, logger: logger).run(reason: argumentValue("--reason") ?? "manual")
    printJSON(["exit_code": result.exitCode, "message": result.message, "ok": result.exitCode == 0], path: resultPath())
    exit(result.exitCode)
}

private func runDoctor() throws {
    let recordings = try requiredValue("--recordings-dir")
    let codex = try requiredValue("--codex-path")
    let gh = try requiredValue("--gh-path")
    let readable = (try? FileManager.default.contentsOfDirectory(atPath: recordings)) != nil
    let result: [String: Any] = [
        "accessibility": AXIsProcessTrusted(),
        "codex_executable": FileManager.default.isExecutableFile(atPath: codex),
        "full_disk_access": readable,
        "gh_executable": FileManager.default.isExecutableFile(atPath: gh),
        "ok": AXIsProcessTrusted() && readable && FileManager.default.isExecutableFile(atPath: codex) && FileManager.default.isExecutableFile(atPath: gh),
    ]
    printJSON(result, path: resultPath())
    exit((result["ok"] as? Bool) == true ? 0 : 2)
}

private func pushoverCredentialFromClipboard(named name: String) -> String? {
    while true {
        let alert = NSAlert()
        alert.messageText = "Configure Pushover: \(name)"
        alert.informativeText = "Copy the \(name) from Pushover, return here, and click Use Clipboard. The value stays hidden and the clipboard is cleared immediately."
        alert.addButton(withTitle: "Use Clipboard")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return nil }

        let pasteboard = NSPasteboard.general
        let value = pasteboard.string(forType: .string)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        pasteboard.clearContents()
        if validPushoverCredential(value) { return value }

        let invalid = NSAlert()
        invalid.alertStyle = .warning
        invalid.messageText = "Clipboard value is not valid"
        invalid.informativeText = "Copy the \(name) from the Pushover dashboard and try again."
        invalid.runModal()
    }
}

private func runConfigurePushover() throws {
    NSApplication.shared.setActivationPolicy(.accessory)
    NSApplication.shared.activate(ignoringOtherApps: true)

    guard let userKey = pushoverCredentialFromClipboard(named: "User Key"),
          let apiToken = pushoverCredentialFromClipboard(named: "API Token") else {
        printJSON(["configured": false, "ok": false, "cancelled": true], path: resultPath())
        exit(2)
    }
    try PushoverNotifier().save(userKey: userKey, apiToken: apiToken)
    printJSON(["configured": true, "ok": true], path: resultPath())
}

private func runPushoverStatus() throws {
    let configured = try PushoverNotifier().configured()
    printJSON(["configured": configured, "ok": configured], path: resultPath())
    exit(configured ? 0 : 2)
}

private func runPushoverTest() throws {
    let payload = ImportNotificationPayload(
        title: "Voice Memo Agent",
        message: "Pushover notifications are configured and ready.",
        url: nil
    )
    let requestID = try PushoverNotifier().send(payload)
    printJSON(["ok": true, "provider": "pushover", "request_id": requestID], path: resultPath())
}

private func runNotificationPreview() throws {
    let encoded = try requiredValue("--workflow-json")
    guard let data = encoded.data(using: .utf8),
          let result = try? JSONDecoder().decode(WorkflowResult.self, from: data) else {
        printJSON(["ok": true, "should_notify": false])
        return
    }
    let payload: ImportNotificationPayload
    if let imported = result.imports.first {
        payload = importNotificationPayload(from: imported)
    } else if let review = result.reviews?.first {
        payload = reviewNotificationPayload(from: review)
    } else if let failure = result.actionableFailures.first {
        payload = failureNotificationPayload(from: failure)
    } else {
        printJSON(["ok": true, "should_notify": false])
        return
    }
    printJSON([
        "message": payload.message,
        "ok": true,
        "should_notify": true,
        "title": payload.title,
        "url": payload.url ?? NSNull(),
    ])
}

private func runProcessingStartedNotificationPreview() throws {
    let recordingCount = try intValue("--recording-count", default: 1)
    guard recordingCount > 0 else {
        throw AgentError.message("--recording-count must be greater than zero")
    }
    let payload = processingStartedNotificationPayload(recordingCount: recordingCount)
    printJSON([
        "message": payload.message,
        "ok": true,
        "should_notify": true,
        "title": payload.title,
        "url": payload.url ?? NSNull(),
    ])
}

private func runRenameRowScore() throws {
    let description = try requiredValue("--description")
    let score = renameRowScore(
        description: description,
        recordedAt: parseISO8601Date(argumentValue("--recorded-at")),
        duration: argumentValue("--duration").flatMap(Double.init)
    )
    printJSON(["ok": true, "score": score])
}

private func runDemoLogPreview() throws {
    let path = try requiredValue("--demo-log-path")
    let logger = DemoLogger(
        path: path,
        minimumIntervalMilliseconds: try intValue("--demo-log-interval-ms", default: 1500)
    )
    logger.write("🎙️ New memo detected. I’ll process the audio locally before touching your notes.")
    let reader = DemoProgressReader(logger: logger)
    reader.consume(Data("""
    voice-memo-demo:listening
    voice-memo-demo:qualified
    voice-memo-demo:organizing
    voice-memo-demo:drafting
    voice-memo-demo:validated
    voice-memo-demo:imported

    """.utf8))
    reader.finish()
    logger.flush()
    printJSON(["log_path": path, "ok": true])
}

private func printHelp() {
    print("Voice Memo Agent")
    print("  watch --recordings-dir PATH --repo PATH --codex-path PATH --gh-path PATH --node-path PATH --python-path PATH --sync-script PATH [--sync-timeout-seconds N]")
    print("  sync --repo PATH --codex-path PATH --gh-path PATH --node-path PATH --python-path PATH --sync-script PATH [--sync-timeout-seconds N]")
    print("  doctor --recordings-dir PATH --codex-path PATH --gh-path PATH")
    print("  classify-event --path PATH [--created] [--is-file]")
    print("  configure-pushover | pushover-status | pushover-test")
    print("  notification-preview --workflow-json JSON")
    print("  demo-log-preview --demo-log-path PATH [--demo-log-interval-ms N]")
    print("  processing-started-notification-preview [--recording-count N]")
    print("  rename-row-score --description TEXT [--recorded-at DATE] [--duration SECONDS]")
    print("  --memo-id ID --current-title TITLE --new-title TITLE")
    print("  --check-accessibility | --request-accessibility")
}

do {
    if hasArgument("--help") || CommandLine.arguments.count == 1 {
        printHelp()
    } else if hasArgument("--check-accessibility") {
        let trusted = AXIsProcessTrusted()
        printJSON(["accessibility": trusted, "ok": trusted], path: resultPath())
        exit(trusted ? 0 : 2)
    } else if hasArgument("--request-accessibility") {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        let trusted = AXIsProcessTrustedWithOptions(options)
        printJSON(["accessibility": trusted, "ok": trusted, "requested": true], path: resultPath())
        exit(trusted ? 0 : 2)
    } else if hasArgument("configure-pushover") {
        try runConfigurePushover()
    } else if hasArgument("pushover-status") {
        try runPushoverStatus()
    } else if hasArgument("pushover-test") {
        try runPushoverTest()
    } else if hasArgument("notification-preview") {
        try runNotificationPreview()
    } else if hasArgument("demo-log-preview") {
        try runDemoLogPreview()
    } else if hasArgument("processing-started-notification-preview") {
        try runProcessingStartedNotificationPreview()
    } else if hasArgument("rename-row-score") {
        try runRenameRowScore()
    } else if hasArgument("watch") {
        try runWatch()
    } else if hasArgument("sync") {
        try runSyncOnce()
    } else if hasArgument("doctor") {
        try runDoctor()
    } else if hasArgument("classify-event") {
        let path = try requiredValue("--path")
        var flags: FSEventStreamEventFlags = 0
        if hasArgument("--created") { flags |= FSEventStreamEventFlags(kFSEventStreamEventFlagItemCreated) }
        if hasArgument("--is-file") { flags |= FSEventStreamEventFlags(kFSEventStreamEventFlagItemIsFile) }
        printJSON(["ok": true, "should_trigger": shouldTriggerMemo(path: path, flags: flags)])
    } else {
        try runRename()
    }
} catch {
    if resultPath() != nil {
        printJSON(["error": String(describing: error), "ok": false], path: resultPath())
    } else {
        fputs("VoiceMemoAgent: \(error)\n", stderr)
    }
    exit(1)
}
