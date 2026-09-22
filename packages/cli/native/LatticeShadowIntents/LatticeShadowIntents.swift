import AppIntents
import Foundation

enum ShadowIntentRunner {
    static func run(_ arguments: [String]) throws -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["shadow"] + arguments

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        try process.run()
        process.waitUntilExit()

        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return String(data: data, encoding: .utf8) ?? ""
    }
}

struct RecallMemoryIntent: AppIntent {
    static let title: LocalizedStringResource = "Recall LatticeShadow Memory"

    @Parameter(title: "Query")
    var query: String

    func perform() async throws -> some IntentResult & ReturnsValue<String> {
        let output = try ShadowIntentRunner.run(["timeline", "--query", query, "--json"])
        return .result(value: output)
    }
}

struct PasteMemoryIntent: AppIntent {
    static let title: LocalizedStringResource = "Paste LatticeShadow Recall"

    @Parameter(title: "Query")
    var query: String

    func perform() async throws -> some IntentResult & ReturnsValue<String> {
        let output = try ShadowIntentRunner.run(["paste", query])
        return .result(value: output)
    }
}

struct SummarizeContextIntent: AppIntent {
    static let title: LocalizedStringResource = "Summarize Current Context"

    func perform() async throws -> some IntentResult & ReturnsValue<String> {
        let output = try ShadowIntentRunner.run(["now", "--json"])
        return .result(value: output)
    }
}

struct ForgetMemoryIntent: AppIntent {
    static let title: LocalizedStringResource = "Forget LatticeShadow Memory"

    @Parameter(title: "Memory ID")
    var id: String

    func perform() async throws -> some IntentResult & ReturnsValue<String> {
        let output = try ShadowIntentRunner.run(["forget", "--id", id, "--yes"])
        return .result(value: output)
    }
}

struct OpenSourceContextIntent: AppIntent {
    static let title: LocalizedStringResource = "Open LatticeShadow Source Context"

    @Parameter(title: "Query")
    var query: String

    func perform() async throws -> some IntentResult & ReturnsValue<String> {
        let output = try ShadowIntentRunner.run(["open-context", query])
        return .result(value: output)
    }
}
