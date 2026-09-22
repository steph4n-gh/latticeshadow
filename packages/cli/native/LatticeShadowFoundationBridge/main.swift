import Foundation

#if canImport(FoundationModels)
import FoundationModels
#endif

struct BridgeRequest: Decodable {
    var task: String
    var events: [String]?
    var text: String?
}

func extractiveSummary(_ lines: [String]) -> String {
    if lines.isEmpty {
        return "No memory events found."
    }

    let preview = lines
        .prefix(5)
        .map { line in
            let collapsed = line.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
            if collapsed.count > 220 {
                return String(collapsed.prefix(217)) + "..."
            }
            return collapsed
        }
        .joined(separator: " ")

    return "\(lines.count) event(s). Most relevant: \(preview)"
}

@main
struct LatticeShadowFoundationBridge {
    static func main() async {
        do {
            let input = FileHandle.standardInput.readDataToEndOfFile()
            let data = input.isEmpty ? Data(#"{"task":"summarize","events":[]}"#.utf8) : input
            let request = try JSONDecoder().decode(BridgeRequest.self, from: data)
            let lines = request.events ?? request.text.map { [$0] } ?? []

            guard request.task == "summarize" else {
                FileHandle.standardError.write(Data("unsupported task\n".utf8))
                Foundation.exit(2)
            }

            #if canImport(FoundationModels)
            if #available(macOS 26.0, *) {
                do {
                    if let summary = try await foundationSummary(lines), !summary.isEmpty {
                        print(summary)
                        return
                    }
                } catch {
                    // Keep the bridge useful when the framework exists but the local model is unavailable.
                }
            }
            #endif

            print(extractiveSummary(lines))
        } catch {
            FileHandle.standardError.write(Data("\(error)\n".utf8))
            Foundation.exit(1)
        }
    }

    #if canImport(FoundationModels)
    @available(macOS 26.0, *)
    static func foundationSummary(_ lines: [String]) async throws -> String? {
        if lines.isEmpty {
            return "No memory events found."
        }

        let prompt = """
        Summarize these private local memory events in five concise bullets or fewer.
        Preserve visible event ids. Do not invent facts.

        \(lines.joined(separator: "\n"))
        """

        let session = LanguageModelSession()
        let response = try await session.respond(to: prompt)
        return String(describing: response.content).trimmingCharacters(in: .whitespacesAndNewlines)
    }
    #endif
}
