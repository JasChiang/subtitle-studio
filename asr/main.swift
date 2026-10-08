// apple-asr — on-device Apple SpeechTranscriber → word-level timed JSON.
//
//   apple-asr <audio-file> [--locale en-US] [--list-locales]
//
// stdout: {"locale":…, "segments":[{text,start,end,words:[{text,start,end}]}]}
// stderr: "PROGRESS <seconds>" lines as finalized segments arrive, plus diagnostics.
// Uses the SpeechAnalyzer / SpeechTranscriber API (macOS 26+); no SFSpeechRecognizer authorization needed.
import AVFoundation
import Foundation
import Speech

struct Word: Encodable { let text: String; let start: Double?; let end: Double? }
struct Segment: Encodable { let text: String; let start: Double; let end: Double; let words: [Word] }
struct Output: Encodable { let locale: String; let duration: Double; let segments: [Segment] }

func eprint(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

func matchLocale(_ wanted: Locale, in supported: [Locale]) -> Locale? {
    let id = wanted.identifier(.bcp47)
    if let exact = supported.first(where: { $0.identifier(.bcp47) == id }) { return exact }
    guard let lang = wanted.language.languageCode?.identifier else { return nil }
    let same = supported.filter { $0.language.languageCode?.identifier == lang }
    return same.first { $0.region?.identifier == wanted.region?.identifier } ?? same.first
}

@main
struct AppleASR {
    static func main() async {
        var args = Array(CommandLine.arguments.dropFirst())
        var localeID = "en-US"
        if let i = args.firstIndex(of: "--locale"), i + 1 < args.count {
            localeID = args[i + 1]; args.removeSubrange(i...(i + 1))
        }
        let supported = await SpeechTranscriber.supportedLocales
        if args.contains("--list-locales") {
            print(supported.map { $0.identifier(.bcp47) }.sorted().joined(separator: "\n"))
            return
        }
        guard let path = args.first else {
            eprint("usage: apple-asr <audio-file> [--locale en-US] [--list-locales]"); exit(2)
        }
        do {
            try await run(url: URL(fileURLWithPath: path), localeID: localeID, supported: supported)
        } catch {
            eprint("ERROR \(error.localizedDescription)"); exit(1)
        }
    }

    static func run(url: URL, localeID: String, supported: [Locale]) async throws {
        guard let locale = matchLocale(Locale(identifier: localeID), in: supported) else {
            throw NSError(domain: "apple-asr", code: 3, userInfo: [NSLocalizedDescriptionKey:
                "locale \(localeID) not supported by SpeechTranscriber"])
        }
        eprint("LOCALE \(locale.identifier(.bcp47))")
        let transcriber = SpeechTranscriber(locale: locale, transcriptionOptions: [],
                                            reportingOptions: [], attributeOptions: [.audioTimeRange])
        if let install = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
            eprint("INSTALL downloading speech model for \(locale.identifier(.bcp47))")
            try await install.downloadAndInstall()
        }
        let file = try AVAudioFile(forReading: url)
        let duration = Double(file.length) / file.processingFormat.sampleRate
        eprint("DURATION \(duration)")

        let analyzer = SpeechAnalyzer(modules: [transcriber])
        let collector = Task { () throws -> [Segment] in
            var segs: [Segment] = []
            for try await result in transcriber.results {
                let attributed = result.text
                var words: [Word] = []
                for run in attributed.runs {
                    let t = String(attributed[run.range].characters).trimmingCharacters(in: .whitespacesAndNewlines)
                    if t.isEmpty { continue }
                    let r = run.audioTimeRange
                    words.append(Word(text: t, start: r?.start.seconds, end: r.map { ($0.start + $0.duration).seconds }))
                }
                let text = String(attributed.characters).trimmingCharacters(in: .whitespacesAndNewlines)
                if !text.isEmpty {
                    segs.append(Segment(text: text, start: result.range.start.seconds,
                                        end: result.range.end.seconds, words: words))
                    eprint("PROGRESS \(result.range.end.seconds)")
                }
            }
            return segs
        }
        if let last = try await analyzer.analyzeSequence(from: file) {
            try await analyzer.finalizeAndFinish(through: last)
        } else {
            await analyzer.cancelAndFinishNow()
        }
        let segments = try await collector.value
        let enc = JSONEncoder()
        enc.outputFormatting = [.withoutEscapingSlashes]
        FileHandle.standardOutput.write(try enc.encode(Output(locale: locale.identifier(.bcp47),
                                                              duration: duration, segments: segments)))
    }
}
