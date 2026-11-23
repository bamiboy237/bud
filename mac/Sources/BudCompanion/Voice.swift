import AVFoundation
import AppKit
import Carbon.HIToolbox
import Contacts
import FluidAudio
import OSLog
import Synchronization

private let log = Logger(subsystem: "bud.companion", category: "voice")

/// Where hold-to-talk is.
enum VoiceState: Equatable {
    /// The models aren't on this Mac yet; the menu offers "Set up voice…".
    case off
    /// Downloading or loading the models, 0 to 1.
    case preparing(Double)
    case ready
    case listening
    case transcribing
    case failed(String)
}

protocol VoiceRecorder: AnyObject {
    func start(level: @escaping @Sendable (Float) -> Void) throws
    func stop() -> Clip
}

protocol VoiceTranscriber: Sendable {
    func transcribe(_ clip: Clip) async throws -> String
}

/// Hold a key, talk, let go: your words go to bud as a message. Everything runs on this Mac.
///
/// The microphone records while the key is down. On release, Parakeet (through FluidAudio)
/// turns the whole clip into text in about a fifth of a second; batch, not streaming, because
/// you hear nothing until you let go anyway, and batch is the more accurate model. A CTC
/// vocabulary boost then corrects names from Contacts, and a one-word rule fixes bud's own
/// name, which the model hears as "bod".
@MainActor @Observable
final class Voice {
    static let shared = Voice()

    private(set) var state: VoiceState = .off
    /// How loud the microphone is, 0 to 1, for the level pill.
    private(set) var level: Float = 0
    /// Which key you hold: ⌥ Space, or right ⌥ when another app owns ⌥ Space.
    private(set) var keyName = "⌥ Space"
    /// The right ⌥ fallback needs Accessibility; the menu asks for it when this is true.
    private(set) var needsAccessibility = false

    @ObservationIgnored private var transcriber: (any VoiceTranscriber)?
    @ObservationIgnored private let companion: Companion
    @ObservationIgnored private let recorder: any VoiceRecorder
    @ObservationIgnored private let registerHotkey:
        @MainActor (Int, Int, @escaping () -> Void, @escaping () -> Void) -> (any HotkeyToken)?
    @ObservationIgnored private let isDownloaded: () -> Bool
    @ObservationIgnored private let requestMicAccess: () async -> Bool
    @ObservationIgnored private let loadContacts: (Bool) async -> [String]
    @ObservationIgnored private let loadTranscriber:
        ([String], @escaping @Sendable (Double) -> Void) async throws -> any VoiceTranscriber
    @ObservationIgnored private let rightOptionAllowed: @MainActor () -> Bool
    @ObservationIgnored private let makeRightOptionHold:
        @MainActor (@escaping () -> Void, @escaping () -> Void) -> RightOptionHold
    @ObservationIgnored private let playSound: @MainActor (TalkSound) -> Void
    @ObservationIgnored private var talkKey: (any HotkeyToken)?
    @ObservationIgnored private var optionHold: RightOptionHold?
    @ObservationIgnored private var escapeKeys: [any HotkeyToken] = []
    /// What the field was for when this recording started: a message, or an answer.
    @ObservationIgnored private var destination: InputPurpose = .ask
    @ObservationIgnored private(set) var setupTask: Task<Void, Never>?
    @ObservationIgnored private(set) var transcriptionTask: Task<Void, Never>?

    init(
        companion: Companion = .shared,
        recorder: any VoiceRecorder = Recorder(),
        registerHotkey: @escaping @MainActor (
            Int, Int, @escaping () -> Void, @escaping () -> Void
        ) -> (any HotkeyToken)? = { Hotkey(key: $0, modifiers: $1, onPress: $2, onRelease: $3) },
        isDownloaded: @escaping () -> Bool = { Transcriber.downloaded },
        requestMicAccess: @escaping () async -> Bool = {
            await AVCaptureDevice.requestAccess(for: .audio)
        },
        loadContacts: @escaping (Bool) async -> [String] = { await ContactNames.load(ask: $0) },
        loadTranscriber: @escaping (
            [String], @escaping @Sendable (Double) -> Void
        ) async throws -> any VoiceTranscriber = {
            try await Transcriber.load(names: $0, progress: $1)
        },
        rightOptionAllowed: @escaping @MainActor () -> Bool = { RightOptionHold.allowed },
        makeRightOptionHold: @escaping @MainActor (
            @escaping () -> Void, @escaping () -> Void
        ) -> RightOptionHold = { RightOptionHold(onPress: $0, onRelease: $1) },
        playSound: @escaping @MainActor (TalkSound) -> Void = { $0.play() }
    ) {
        self.companion = companion
        self.recorder = recorder
        self.registerHotkey = registerHotkey
        self.isDownloaded = isDownloaded
        self.requestMicAccess = requestMicAccess
        self.loadContacts = loadContacts
        self.loadTranscriber = loadTranscriber
        self.rightOptionAllowed = rightOptionAllowed
        self.makeRightOptionHold = makeRightOptionHold
        self.playSound = playSound
    }

    /// Takes the key, and loads the models if they were set up before.
    func start() {
        talkKey = registerHotkey(kVK_Space, optionKey, press, release)
        if talkKey == nil {
            log.info("⌥ Space is taken; using the right Option key")
            keyName = "right ⌥"
            needsAccessibility = !rightOptionAllowed()
            optionHold = makeRightOptionHold(press, release)
        }
        if isDownloaded() { prepare(askForContacts: false) }
    }

    /// "Set up voice…": asks for the microphone (and Contacts, for names), then downloads.
    func setUp() {
        setupTask = Task {
            guard await requestMicAccess() else {
                state = .failed("Microphone access is off. Turn it on in System Settings, Privacy & Security, Microphone.")
                return
            }
            await prepareAndWait(askForContacts: true)
        }
    }

    /// Opens the Accessibility prompt, then picks the key up once it's allowed. A monitor made
    /// before the permission was granted stays blind, so it's made again.
    func allowRightOption() {
        RightOptionHold.askForPermission()
        Task {
            for _ in 0..<90 where !rightOptionAllowed() {
                try? await Task.sleep(for: .seconds(2))
            }
            guard rightOptionAllowed() else { return }
            optionHold?.stop()
            optionHold = makeRightOptionHold(press, release)
            needsAccessibility = false
        }
    }

    /// At launch nothing is asked: Contacts are used only if you allowed them during setup.
    private func prepare(askForContacts: Bool) {
        setupTask = Task { await prepareAndWait(askForContacts: askForContacts) }
    }

    private func prepareAndWait(askForContacts: Bool) async {
        if case .preparing = state { return }
        state = .preparing(0)
        let names = await loadContacts(askForContacts)
        do {
            transcriber = try await loadTranscriber(names) { [weak self] fraction in
                // Updates arrive out of order across tasks; the bar only moves forward.
                Task { @MainActor in
                    guard let self else { return }
                    if case .preparing(let shown) = self.state, fraction > shown {
                        self.state = .preparing(fraction)
                    }
                }
            }
            state = .ready
        } catch {
            log.error("voice setup failed: \(error.localizedDescription, privacy: .public)")
            state = .failed("Couldn't set up voice: \(error.localizedDescription)")
        }
    }

    // MARK: Talking

    func press() {
        switch state {
        case .ready:
            // One held recording at a time: a second can't replace words not yet sent.
            guard companion.unsentSpeech == nil else {
                companion.nudge("Send or clear the words bud is holding first.")
                return
            }
            begin()
        case .off, .failed:
            companion.nudge("Voice isn't set up yet. Choose Set up voice… in the menu bar.")
        case .preparing:
            companion.nudge("Voice is still getting ready.")
        case .listening, .transcribing:
            break  // A held key repeats; one press is one recording.
        }
    }

    private func begin() {
        do {
            try recorder.start { [weak self] level in
                Task { @MainActor in self?.level = level }
            }
        } catch {
            log.error("mic failed: \(error.localizedDescription, privacy: .public)")
            companion.nudge("bud couldn't use the microphone.")
            return
        }
        state = .listening
        destination = companion.inputPurpose
        companion.hearing = true
        playSound(.start)
        // Esc drops the recording; taken only while listening, so it's never stolen otherwise.
        // Option is still held, so it arrives as ⌥ Esc; plain Esc covers a key let go early.
        escapeKeys = [optionKey, 0].compactMap { registerHotkey(kVK_Escape, $0, cancel, {}) }
    }

    func release() {
        guard state == .listening else { return }
        let clip = finishListening()
        playSound(.stop)
        // A tap, not a sentence: nothing to send.
        guard clip.seconds >= 0.3, let transcriber else {
            state = .ready
            return
        }
        state = .transcribing
        let destination = destination
        transcriptionTask = Task {
            do {
                let text = try await transcriber.transcribe(clip)
                state = .ready
                if !text.isEmpty { companion.say(text, meant: destination) }
            } catch {
                log.error("transcription failed: \(error.localizedDescription, privacy: .public)")
                state = .ready
                companion.nudge("bud didn't catch that. Try again?")
            }
        }
    }

    func cancel() {
        guard state == .listening else { return }
        _ = finishListening()
        state = .ready
    }

    private func finishListening() -> Clip {
        escapeKeys.forEach { $0.unregister() }
        escapeKeys = []
        companion.hearing = false
        level = 0
        return recorder.stop()
    }
}

enum RecorderError: LocalizedError {
    case noMicrophone
    var errorDescription: String? { "No microphone is available." }
}

/// Audio as recorded: mono samples at the microphone's own rate.
struct Clip: Sendable {
    var samples: [Float]
    var rate: Double
    var seconds: Double { rate > 0 ? Double(samples.count) / rate : 0 }
}

/// The microphone. The tap runs on an audio thread, so samples land in a lock-guarded buffer
/// and only a loudness number crosses back to the main thread.
final class Recorder: VoiceRecorder {
    private let engine = AVAudioEngine()
    private let sink = Sink()

    func start(level: @escaping @Sendable (Float) -> Void) throws {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        // A Mac with no microphone reports an empty format, and tapping it raises an
        // Objective-C exception Swift can't catch; refuse first instead.
        guard format.sampleRate > 0, format.channelCount > 0 else { throw RecorderError.noMicrophone }
        sink.reset(rate: format.sampleRate)
        input.removeTap(onBus: 0)
        let sink = sink
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
            level(sink.append(buffer))
        }
        engine.prepare()
        try engine.start()
    }

    func stop() -> Clip {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        return sink.take()
    }

    private final class Sink: Sendable {
        private let clip = Mutex(Clip(samples: [], rate: 0))

        func reset(rate: Double) {
            clip.withLock { $0 = Clip(samples: [], rate: rate) }
        }

        /// Keeps the first channel and returns the buffer's loudness, 0 to 1.
        func append(_ buffer: AVAudioPCMBuffer) -> Float {
            guard let channel = buffer.floatChannelData?[0] else { return 0 }
            let frames = Array(UnsafeBufferPointer(start: channel, count: Int(buffer.frameLength)))
            clip.withLock { $0.samples += frames }
            let power = frames.reduce(0) { $0 + $1 * $1 } / Float(max(frames.count, 1))
            // Speech sits around -40 to -10 dBFS; map that range onto 0 to 1.
            let decibels = 10 * log10(max(power, 1e-10))
            return min(max((decibels + 50) / 40, 0), 1)
        }

        func take() -> Clip {
            clip.withLock { clip in
                defer { clip.samples = [] }
                return clip
            }
        }
    }
}

/// Parakeet and the name boost, loaded once and used from one place at a time.
actor Transcriber: VoiceTranscriber {
    private let asr: AsrManager
    private let boost: VocabularyBoostingSession?

    private init(asr: AsrManager, boost: VocabularyBoostingSession?) {
        self.asr = asr
        self.boost = boost
    }

    /// Whether "Set up voice…" has run on this Mac before.
    nonisolated static var downloaded: Bool {
        AsrModels.modelsExist(at: AsrModels.defaultCacheDirectory(for: .v3), version: .v3)
    }

    /// Downloads (the first time) and loads the models: Parakeet v3 (about 470 MB) for the
    /// words, and the small CTC model (about 100 MB) that checks names against the audio.
    static func load(names: [String], progress: @escaping @Sendable (Double) -> Void) async throws -> Transcriber {
        let models = try await AsrModels.downloadAndLoad(version: .v3) { p in progress(p.fractionCompleted * 0.8) }
        let asr = AsrManager(config: .default)
        try await asr.loadModels(models)
        // The boost only helps; voice works without it.
        var boost: VocabularyBoostingSession?
        if !names.isEmpty {
            do {
                let ctc = try await CtcModels.downloadAndLoad(variant: .ctc110m) { p in
                    progress(0.8 + p.fractionCompleted * 0.2)
                }
                // Settings FluidAudio measured for dictation: a name must win on the sound
                // alone, so ordinary words that look like a name are left alone.
                boost = try await VocabularyBoostingSession(
                    vocabulary: CustomVocabularyContext(terms: names.map { CustomVocabularyTerm(text: $0) }),
                    ctcModels: ctc,
                    config: VocabularyRescorer.Config(spotterRescueEnabled: false),
                    thresholds: .init(cbw: 0, minSimilarity: 0.7))
            } catch {
                log.error("name boost unavailable: \(error.localizedDescription, privacy: .public)")
            }
        }
        progress(1)
        return Transcriber(asr: asr, boost: boost)
    }

    func transcribe(_ clip: Clip) async throws -> String {
        let audio = try Self.resample(clip)
        var decoder = TdtDecoderState.make(decoderLayers: await asr.decoderLayerCount)
        let result = try await asr.transcribe(audio, decoderState: &decoder)
        var text = result.text
        if let boost,
           let boosted = await boost.rescore(text: text, tokenTimings: result.tokenTimings ?? [], audioSamples: audio)
        {
            text = boosted.text
        }
        return Self.fixName(text).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Parakeet hears "bud" as "bod", a word nobody says to their assistant; the boost can't
    /// fix it safely, because loosening it lets "bud" replace "but". So the whole word is
    /// swapped back. "body" and "bodega" don't match.
    static func fixName(_ text: String) -> String {
        text.replacing(/\b[Bb][Oo][Dd]\b/, with: "bud")
    }

    /// The microphone's rate (usually 48 kHz) down to the 16 kHz mono the model expects.
    private static func resample(_ clip: Clip) throws -> [Float] {
        guard let format = AVAudioFormat(standardFormatWithSampleRate: clip.rate, channels: 1),
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(clip.samples.count))
        else { return [] }
        buffer.frameLength = AVAudioFrameCount(clip.samples.count)
        clip.samples.withUnsafeBufferPointer { source in
            buffer.floatChannelData?[0].update(from: source.baseAddress!, count: clip.samples.count)
        }
        return try AudioConverter().resampleBuffer(buffer)
    }
}

/// Names from Contacts, for the boost: full names and distinct first names. Empty when
/// access is refused, which only costs name accuracy.
enum ContactNames {
    static func load(ask: Bool) async -> [String] {
        let store = CNContactStore()
        let allowed = ask
            ? (try? await store.requestAccess(for: .contacts)) == true
            : CNContactStore.authorizationStatus(for: .contacts) == .authorized
        guard allowed else { return [] }
        return await Task.detached {
            var names = Set<String>()
            let keys = [CNContactGivenNameKey, CNContactFamilyNameKey] as [CNKeyDescriptor]
            try? store.enumerateContacts(with: CNContactFetchRequest(keysToFetch: keys)) { contact, _ in
                let given = contact.givenName.trimmingCharacters(in: .whitespaces)
                let full = "\(given) \(contact.familyName)".trimmingCharacters(in: .whitespaces)
                for name in [given, full] where name.count >= 3 { names.insert(name) }
            }
            // FluidAudio is tested to about 230 terms; past that, matching gets slower and looser.
            // Contacts says nothing about who matters most, so with more names than that the
            // ones kept are simply the first 200 in alphabetical order.
            return Array(names.sorted().prefix(200))
        }.value
    }
}

/// Soft ticks when listening starts and stops, so you know without looking.
enum TalkSound {
    case start, stop

    @MainActor func play() {
        let sound = NSSound(named: self == .start ? "Tink" : "Pop")
        sound?.volume = 0.25
        sound?.play()
    }
}
