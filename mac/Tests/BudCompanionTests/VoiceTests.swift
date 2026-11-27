import AVFoundation
import AppKit
import Carbon.HIToolbox
import Foundation
import Testing
@testable import BudCompanion

@Suite("Voice hold-to-talk and routing")
@MainActor
struct VoiceTests {
    // MARK: - Test Doubles

    final class FakeHotkeyToken: HotkeyToken {
        let key: Int
        let modifiers: Int
        let onPress: () -> Void
        let onRelease: () -> Void
        var unregistered = false

        init(key: Int, modifiers: Int, onPress: @escaping () -> Void, onRelease: @escaping () -> Void) {
            self.key = key
            self.modifiers = modifiers
            self.onPress = onPress
            self.onRelease = onRelease
        }

        func unregister() {
            unregistered = true
        }
    }

    final class FakeRecorder: VoiceRecorder {
        var startError: Error?
        var nextClip = Clip(samples: Array(repeating: 0.1, count: 16_000), rate: 16_000)
        private(set) var startCount = 0
        private(set) var stopCount = 0
        private var levelHandler: (@Sendable (Float) -> Void)?

        func start(level: @escaping @Sendable (Float) -> Void) throws {
            startCount += 1
            if let startError { throw startError }
            levelHandler = level
        }

        func emitLevel(_ value: Float) {
            levelHandler?(value)
        }

        func stop() -> Clip {
            stopCount += 1
            return nextClip
        }
    }

    actor FakeTranscriber: VoiceTranscriber {
        var result: Result<String, Error> = .success("hello bud")
        private(set) var calls: [Clip] = []
        private let pause: Bool
        private var paused: CheckedContinuation<Void, Never>?
        nonisolated let (started, startedEvents) = AsyncStream<Void>.makeStream()

        init(pause: Bool = false) {
            self.pause = pause
        }

        func setResult(_ next: Result<String, Error>) {
            result = next
        }

        func transcribe(_ clip: Clip) async throws -> String {
            calls.append(clip)
            if pause {
                await withCheckedContinuation {
                    paused = $0
                    startedEvents.yield(())
                }
            }
            return try result.get()
        }

        func finish() {
            paused?.resume()
            paused = nil
        }
    }

    final class StubTransport: @unchecked Sendable {
        struct RecordedRequest {
            let method: String
            let path: String
            let body: [String: Any]
        }

        private let lock = NSLock()
        private var handler: @Sendable (URLRequest) throws -> (Data, HTTPURLResponse)
        private(set) var requests: [RecordedRequest] = []

        init(
            statusCode: Int = 200,
            responseBody: [String: Any] = ["ok": true]
        ) {
            let data = (try? JSONSerialization.data(withJSONObject: responseBody)) ?? Data()
            self.handler = { req in
                let response = HTTPURLResponse(
                    url: req.url!, statusCode: statusCode, httpVersion: nil, headerFields: nil)!
                return (data, response)
            }
        }

        func setHandler(_ handler: @escaping @Sendable (URLRequest) throws -> (Data, HTTPURLResponse)) {
            lock.withLock { self.handler = handler }
        }

        func makeConnection(scope: String = "scope-1") -> BudConnection {
            BudConnection(
                base: URL(string: "http://127.0.0.1:8420")!,
                token: String(repeating: "a", count: 43),
                scope: scope
            ) { [self] request in
                let json = request.httpBody.flatMap {
                    try? JSONSerialization.jsonObject(with: $0) as? [String: Any]
                } ?? [:]
                let recorded = RecordedRequest(
                    method: request.httpMethod ?? "",
                    path: request.url?.path ?? "",
                    body: json
                )
                let currentHandler = lock.withLock {
                    requests.append(recorded)
                    return handler
                }
                return try currentHandler(request)
            }
        }
    }

    private func makeVoice(
        companion: Companion = Companion(),
        recorder: FakeRecorder = FakeRecorder(),
        transcriber: FakeTranscriber = FakeTranscriber(),
        isDownloaded: Bool = false,
        micAllowed: Bool = true,
        micAuthorization: Box<AVAuthorizationStatus> = Box(.authorized),
        hasMicrophone: Box<Bool> = Box(true),
        notificationCenter: NotificationCenter = NotificationCenter(),
        silenceThreshold: Duration = .milliseconds(2_500),
        allowTalkKey: Bool = true,
        rightOptionAllowed: Bool = true,
        loadError: Error? = nil
    ) -> (
        voice: Voice,
        companion: Companion,
        recorder: FakeRecorder,
        transcriber: FakeTranscriber,
        tokens: Box<[FakeHotkeyToken]>
    ) {
        let tokens = Box<[FakeHotkeyToken]>([])
        let voice = Voice(
            companion: companion,
            recorder: recorder,
            registerHotkey: { key, modifiers, onPress, onRelease in
                if key == kVK_Space, !allowTalkKey { return nil }
                let token = FakeHotkeyToken(
                    key: key, modifiers: modifiers, onPress: onPress, onRelease: onRelease)
                tokens.value.append(token)
                return token
            },
            isDownloaded: { isDownloaded },
            requestMicAccess: { micAllowed },
            microphoneAuthorization: { micAuthorization.value },
            hasMicrophone: { hasMicrophone.value },
            notificationCenter: notificationCenter,
            silenceThreshold: silenceThreshold,
            loadContacts: { _ in [] },
            loadTranscriber: { _, progress in
                if let loadError { throw loadError }
                progress(0.8)
                progress(0.3)  // Out-of-order lower value must not move progress backwards.
                progress(1.0)
                return transcriber
            },
            rightOptionAllowed: { rightOptionAllowed },
            makeRightOptionHold: { onPress, onRelease in
                RightOptionHold(onPress: onPress, onRelease: onRelease, monitorEvents: false)
            },
            playSound: { _ in }
        )
        return (voice, companion, recorder, transcriber, tokens)
    }

    final class Box<T> {
        var value: T
        init(_ value: T) { self.value = value }
    }

    // MARK: - State Machine & Setup

    @Test
    func startRegistersHotkeyAndPreparesOnlyWhenDownloaded() async {
        let notDownloaded = makeVoice(isDownloaded: false)
        notDownloaded.voice.start()
        #expect(notDownloaded.voice.state == .off)
        #expect(notDownloaded.voice.keyName == "⌥ Space")
        #expect(!notDownloaded.voice.needsAccessibility)
        #expect(notDownloaded.tokens.value.count == 1)
        #expect(notDownloaded.tokens.value[0].key == kVK_Space)
        #expect(notDownloaded.tokens.value[0].modifiers == optionKey)

        let downloaded = makeVoice(isDownloaded: true)
        downloaded.voice.start()
        await downloaded.voice.setupTask?.value
        #expect(downloaded.voice.state == .ready)
    }

    @Test
    func startFallsBackToRightOptionWhenOptionSpaceIsTaken() {
        let env = makeVoice(allowTalkKey: false, rightOptionAllowed: false)
        env.voice.start()
        #expect(env.voice.keyName == "right ⌥")
        #expect(env.voice.needsAccessibility)
        #expect(env.tokens.value.isEmpty)
    }

    @Test
    func setupHandlesDeniedMicAndModelLoadFailure() async {
        let denied = makeVoice(micAllowed: false, micAuthorization: Box(.denied))
        denied.voice.setUp()
        await denied.voice.setupTask?.value
        guard case .failed(let micMessage) = denied.voice.state else {
            Issue.record("Expected .failed when mic access is denied, got \(denied.voice.state)")
            return
        }
        #expect(micMessage.contains("Microphone access is off"))
        #expect(denied.voice.microphoneDenied)
        #expect(!denied.voice.microphoneUnavailable)

        let failedLoad = makeVoice(loadError: RecorderError.noMicrophone)
        failedLoad.voice.setUp()
        await failedLoad.voice.setupTask?.value
        guard case .failed(let loadMessage) = failedLoad.voice.state else {
            Issue.record("Expected .failed when model load fails, got \(failedLoad.voice.state)")
            return
        }
        #expect(loadMessage.contains("Couldn't set up voice"))
    }

    @Test
    func pressNudgesWhenNotReadyOrUnsentSpeechExists() async {
        let env = makeVoice(isDownloaded: true)
        env.voice.press()
        #expect(env.companion.notices.last?.text.contains("Set up voice") == true)
        #expect(env.recorder.startCount == 0)

        env.voice.start()
        await env.voice.setupTask?.value
        #expect(env.voice.state == .ready)

        // Hold unsent speech by saying while disconnected.
        env.companion.say("unsent words", meant: .ask)
        #expect(env.companion.unsentSpeech == "unsent words")

        env.voice.press()
        #expect(env.voice.state == .ready)
        #expect(env.recorder.startCount == 0)
        #expect(env.companion.notices.last?.text == "Send or clear the words bud is holding first.")
    }

    @Test
    func recorderReportsMicrophoneDisconnectionAndCanRetry() async {
        let env = makeVoice(isDownloaded: true)
        env.recorder.startError = RecorderError.noMicrophone
        env.voice.start()
        await env.voice.setupTask?.value

        env.voice.press()
        #expect(env.voice.state == .ready)
        #expect(!env.companion.hearing)
        #expect(!env.voice.microphoneDenied)
        #expect(env.voice.microphoneUnavailable)
        #expect(env.companion.notices.last?.text == "No microphone is available. Connect a microphone and try again.")

        env.recorder.startError = nil
        env.voice.press()
        #expect(!env.voice.microphoneUnavailable)
        #expect(env.voice.state == .listening)
        #expect(env.voice.cancelActiveDictation())
    }

    @Test(arguments: [AVAuthorizationStatus.denied, .restricted])
    func activationRechecksPermissionAndSeparatesDenialFromMissingHardware(
        unavailableAuthorization: AVAuthorizationStatus
    ) async {
        let permission = Box(unavailableAuthorization)
        let available = Box(false)
        let notifications = NotificationCenter()
        let env = makeVoice(
            isDownloaded: true, micAuthorization: permission, hasMicrophone: available,
            notificationCenter: notifications)
        env.voice.start()
        await env.voice.setupTask?.value

        env.voice.press()
        #expect(env.voice.microphoneDenied)
        #expect(!env.voice.microphoneUnavailable)
        #expect(env.recorder.startCount == 0)
        #expect(env.companion.notices.last?.text.contains("Microphone access is off") == true)

        permission.value = .authorized
        notifications.post(name: NSApplication.didBecomeActiveNotification, object: nil)
        #expect(!env.voice.microphoneDenied)
        #expect(env.voice.microphoneUnavailable)
        env.voice.press()
        #expect(env.recorder.startCount == 0)
        #expect(env.companion.notices.last?.text.contains("Connect a microphone") == true)

        available.value = true
        notifications.post(name: NSApplication.didBecomeActiveNotification, object: nil)
        #expect(!env.voice.microphoneUnavailable)
        env.voice.press()
        #expect(env.voice.state == .listening)
        #expect(env.voice.cancelActiveDictation())
    }

    @Test
    func grantingPermissionInSettingsRestoresSetup() async {
        let permission = Box(AVAuthorizationStatus.denied)
        let notifications = NotificationCenter()
        let env = makeVoice(
            micAllowed: false, micAuthorization: permission, notificationCenter: notifications)
        env.voice.setUp()
        await env.voice.setupTask?.value
        #expect(env.voice.microphoneDenied)

        permission.value = .authorized
        notifications.post(name: NSApplication.didBecomeActiveNotification, object: nil)
        #expect(env.voice.state == .off)
        #expect(!env.voice.microphoneDenied)
    }

    @Test
    func listeningIgnoresRepeatPressAndShortTapSkipsTranscription() async {
        let env = makeVoice(isDownloaded: true)
        env.recorder.nextClip = Clip(samples: Array(repeating: 0.1, count: 1_000), rate: 16_000)
        env.voice.start()
        await env.voice.setupTask?.value

        env.voice.press()
        #expect(env.voice.state == .listening)
        #expect(env.companion.hearing)

        // Key-repeat press while already listening must not restart recording.
        env.voice.press()
        #expect(env.recorder.startCount == 1)

        env.voice.release()
        #expect(env.voice.state == .ready)
        #expect(!env.companion.hearing)
        #expect(env.recorder.stopCount == 1)
        #expect(await env.transcriber.calls.isEmpty)
    }

    @Test
    func releaseTransitionsThroughTranscribingToReadyAndHandlesErrors() async {
        let transport = StubTransport()
        let companion = Companion()
        companion.connectForTesting(transport.makeConnection())
        let env = makeVoice(companion: companion, isDownloaded: true)
        await env.transcriber.setResult(.failure(RecorderError.noMicrophone))
        env.voice.start()
        await env.voice.setupTask?.value

        env.voice.press()
        #expect(env.voice.state == .listening)
        env.voice.release()
        #expect(env.voice.state == .transcribing)
        await env.voice.transcriptionTask?.value

        #expect(env.voice.state == .ready)
        #expect(companion.notices.last?.text == "bud didn't catch that. Try again?")
        #expect(transport.requests.isEmpty)
    }

    @Test
    func releaseIgnoresEmptyTranscript() async {
        let transport = StubTransport()
        let companion = Companion()
        companion.connectForTesting(transport.makeConnection())
        let env = makeVoice(companion: companion, isDownloaded: true)
        await env.transcriber.setResult(.success(""))
        env.voice.start()
        await env.voice.setupTask?.value

        env.voice.press()
        env.voice.release()
        await env.voice.transcriptionTask?.value

        #expect(env.voice.state == .ready)
        #expect(transport.requests.isEmpty)
        #expect(!companion.bubbleOpen)
    }

    // MARK: - Esc Cancel & Right Option Hold

    @Test
    func escapeHotkeyCancelsListeningAndUnregistersEscapeTokens() async {
        let env = makeVoice(isDownloaded: true)
        env.voice.start()
        await env.voice.setupTask?.value

        env.voice.press()
        #expect(env.voice.state == .listening)
        #expect(env.companion.hearing)

        let escTokens = env.tokens.value.filter { $0.key == kVK_Escape }
        #expect(escTokens.count == 2)
        #expect(Set(escTokens.map(\.modifiers)) == Set([optionKey, 0]))

        // Trigger the registered ⌥ Esc callback.
        escTokens[0].onPress()

        #expect(env.voice.state == .ready)
        #expect(!env.companion.hearing)
        #expect(env.voice.level == 0)
        #expect(env.recorder.stopCount == 1)
        #expect(escTokens.allSatisfy { $0.unregistered })
        #expect(await env.transcriber.calls.isEmpty)

        // Cancelling when already ready is a no-op.
        env.voice.release()
        #expect(!env.voice.cancelActiveDictation())
        #expect(env.recorder.stopCount == 1)
    }

    @Test(arguments: [false, true])
    func escapeCancelsTranscriptionAndIgnoresItsLateCompletion(fails: Bool) async throws {
        let transcriber = FakeTranscriber(pause: true)
        await transcriber.setResult(fails ? .failure(RecorderError.noMicrophone) : .success("late words"))
        let env = makeVoice(transcriber: transcriber, isDownloaded: true)
        env.companion.draft = "typed text"
        env.voice.start()
        await env.voice.setupTask?.value
        env.voice.press()
        env.voice.release()
        let task = try #require(env.voice.transcriptionTask)
        var started = transcriber.started.makeAsyncIterator()
        _ = await started.next()
        #expect(env.voice.state == .transcribing)

        let escape = try #require(env.tokens.value.first { $0.key == kVK_Escape && $0.modifiers == 0 })
        #expect(!escape.unregistered)
        escape.onPress()
        #expect(task.isCancelled)
        #expect(env.voice.state == .ready)
        #expect(env.voice.transcriptionTask == nil)
        #expect(escape.unregistered)

        env.voice.press()
        await transcriber.finish()
        await task.value
        #expect(env.voice.state == .listening)
        #expect(env.companion.hearing)
        #expect(env.companion.draft == "typed text")
        #expect(env.companion.unsentSpeech == nil)
        #expect(env.companion.notices.isEmpty)
        #expect(env.voice.cancelActiveDictation())
    }

    @Test
    func silenceClearsOnSpeechAndCanBeDetectedAgain() async {
        let env = makeVoice(isDownloaded: true, silenceThreshold: .milliseconds(80))
        env.voice.start()
        await env.voice.setupTask?.value

        env.voice.press()
        #expect(!env.voice.noInputDetected)
        env.recorder.emitLevel(0.01)
        #expect(await waitUntil { env.voice.noInputDetected })

        env.recorder.emitLevel(0.7)
        #expect(await waitUntil { !env.voice.noInputDetected })
        #expect(env.voice.level > 0)

        env.recorder.emitLevel(0)
        #expect(await waitUntil { env.voice.noInputDetected })
        env.voice.release()
        #expect(!env.voice.noInputDetected)
        #expect(env.voice.level == 0)
        await env.voice.transcriptionTask?.value
    }

    @Test
    func meterSmoothsLevelAndDropsSamplesFromThePreviousRecording() async {
        let env = makeVoice(isDownloaded: true)
        env.voice.start()
        await env.voice.setupTask?.value
        env.voice.press()

        env.recorder.emitLevel(2)
        #expect(await waitUntil { env.voice.level > 0 })
        let first = env.voice.level
        #expect(first < 1)
        env.recorder.emitLevel(1)
        #expect(await waitUntil { env.voice.level > first })
        let peak = env.voice.level
        #expect(peak < 1)
        env.recorder.emitLevel(-1)
        #expect(await waitUntil { env.voice.level < peak })
        #expect(env.voice.level > 0)

        env.recorder.emitLevel(1)
        #expect(env.voice.cancelActiveDictation())
        env.voice.press()
        env.recorder.emitLevel(0)
        try? await Task.sleep(for: .milliseconds(10))
        #expect(env.voice.level == 0)
        #expect(env.voice.cancelActiveDictation())
    }

    private func waitUntil(_ condition: () -> Bool) async -> Bool {
        let deadline = ContinuousClock.now + .seconds(1)
        while !condition(), ContinuousClock.now < deadline {
            try? await Task.sleep(for: .milliseconds(1))
        }
        return condition()
    }

    @Test
    func rightOptionHoldTracksDeviceRightOptionFlagOnly() {
        var presses = 0
        var releases = 0
        let hold = RightOptionHold(
            onPress: { presses += 1 }, onRelease: { releases += 1 }, monitorEvents: false)

        // Left option / generic option without NX_DEVICERALTKEYMASK (0x40) is ignored.
        hold.handle(modifierFlags: [.option])
        #expect(presses == 0)

        // Right option down triggers once even on repeated flagsChanged events.
        let rightOpt = NSEvent.ModifierFlags(rawValue: NSEvent.ModifierFlags.option.rawValue | 0x40)
        hold.handle(modifierFlags: rightOpt)
        hold.handle(modifierFlags: rightOpt)
        #expect(presses == 1)
        #expect(releases == 0)

        // Releasing right option triggers release once.
        hold.handle(modifierFlags: [.option])
        hold.handle(modifierFlags: [])
        #expect(releases == 1)
    }

    // MARK: - Routing & Draft / Retry ID Preservation

    @Test(arguments: [
        ("", "check my calendar"),
        ("Here is a draft", "Here is a draft check my calendar"),
        ("Here is a draft\n", "Here is a draft\ncheck my calendar"),
    ])
    func spokenMessageAppendsToComposerDraftWithoutSending(initial: String, expected: String) async {
        let transport = StubTransport()
        let companion = Companion()
        companion.connectForTesting(transport.makeConnection(), session: "sess-42")
        companion.draft = initial

        let env = makeVoice(companion: companion, isDownloaded: true)
        await env.transcriber.setResult(.success("check my calendar"))
        env.voice.start()
        await env.voice.setupTask?.value

        env.voice.press()
        env.voice.release()
        await env.voice.transcriptionTask?.value
        await companion.sendTask?.value

        #expect(companion.draft == expected)
        #expect(companion.inputOpen)
        #expect(companion.unsentSpeech == nil)
        #expect(transport.requests.isEmpty)
    }

    @Test
    func spokenRevisionAppendsToDraftWhileDisconnected() async {
        let env = makeVoice(isDownloaded: true)
        env.companion.write(.revise("approval-1"))
        env.companion.draft = "please"
        await env.transcriber.setResult(.success("use the other file"))
        env.voice.start()
        await env.voice.setupTask?.value

        env.voice.press()
        env.voice.release()
        await env.voice.transcriptionTask?.value

        #expect(env.companion.inputPurpose == .revise("approval-1"))
        #expect(env.companion.draft == "please use the other file")
        #expect(env.companion.unsentSpeech == nil)
    }

    @Test
    func spokenAnswerRoutesToActiveQuestionFieldInsteadOfSending() async throws {
        let transport = StubTransport()
        let companion = Companion()
        companion.connectForTesting(transport.makeConnection())
        let approval = try #require(Approval([
            "approval_id": "approval-1", "tool": "ask_user", "args": ["question": "With whom?"],
        ], jobs: [:]))
        let flow = companion.questionFlow(for: approval)
        flow.answers.type("existing", at: 0)
        companion.focusQuestion(approval.id, position: 0, focused: true)
        companion.draft = "separate message"

        let env = makeVoice(companion: companion, isDownloaded: true)
        await env.transcriber.setResult(.success("with Priya"))
        env.voice.start()
        await env.voice.setupTask?.value

        env.voice.press()
        env.voice.release()
        await env.voice.transcriptionTask?.value

        #expect(companion.inputPurpose == .ask)
        #expect(companion.draft == "separate message")
        #expect(flow.answers.typed(at: 0) == "existing with Priya")
        #expect(transport.requests.isEmpty)
    }

    @Test
    func spokenTextIsHeldIfTheAnswerFieldLosesFocusWhileSpeaking() async throws {
        let transport = StubTransport()
        let companion = Companion()
        companion.connectForTesting(transport.makeConnection())
        let approval = try #require(Approval([
            "approval_id": "approval-2", "tool": "ask_user", "args": ["question": "Which room?"],
        ], jobs: [:]))
        _ = companion.questionFlow(for: approval)
        companion.focusQuestion(approval.id, position: 0, focused: true)

        let env = makeVoice(companion: companion, isDownloaded: true)
        await env.transcriber.setResult(.success("book the room"))
        env.voice.start()
        await env.voice.setupTask?.value

        env.voice.press()
        companion.focusQuestion(approval.id, position: 0, focused: false)
        env.voice.release()
        await env.voice.transcriptionTask?.value

        #expect(companion.unsentSpeech == "book the room")
        #expect(companion.notices.last?.text == "Things changed while you were talking, so that wasn't sent.")
        #expect(transport.requests.isEmpty)
    }

    @Test
    func failedSpokenMessagePreservesDraftAndReusesMessageIDOnResend() async {
        let transport = StubTransport(statusCode: 500, responseBody: ["detail": "boom"])
        let companion = Companion()
        companion.connectForTesting(transport.makeConnection())
        companion.draft = "keep my typed draft"

        companion.say("spoken request", meant: .ask)
        await companion.sendTask?.value

        #expect(companion.draft == "keep my typed draft")
        #expect(companion.unsentSpeech == "spoken request")
        #expect(transport.requests.count == 1)
        let firstID = transport.requests[0].body["message_id"] as? String
        #expect(firstID?.isEmpty == false)

        // Succeed on resend and verify the exact same message_id is reused.
        transport.setHandler { req in
            let data = try JSONSerialization.data(withJSONObject: ["ok": true])
            return (
                data,
                HTTPURLResponse(url: req.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            )
        }
        companion.resendSpeech()
        await companion.sendTask?.value

        #expect(companion.unsentSpeech == nil)
        #expect(companion.draft == "keep my typed draft")
        #expect(transport.requests.count == 2)
        #expect(transport.requests[1].body["message_id"] as? String == firstID)
    }

    @Test
    func failedTypedMessagePreservesRetryIDAcrossSpokenMessages() async {
        let transport = StubTransport(statusCode: 500, responseBody: ["detail": "offline"])
        let companion = Companion()
        companion.connectForTesting(transport.makeConnection())
        companion.draft = "typed request"

        companion.send()
        await companion.sendTask?.value

        #expect(companion.draft == "typed request")
        #expect(companion.inputOpen)
        #expect(companion.unsent?.text == "typed request")
        let typedID = companion.unsent?.id
        #expect(typedID != nil)
        #expect(transport.requests.count == 1)
        #expect(transport.requests[0].body["message_id"] as? String == typedID)

        // Sending a spoken message in between must NOT clear the typed message's retry ID.
        transport.setHandler { req in
            let data = try JSONSerialization.data(withJSONObject: ["ok": true])
            return (
                data,
                HTTPURLResponse(url: req.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            )
        }
        companion.say("quick voice note", meant: .ask)
        await companion.sendTask?.value
        #expect(companion.unsent?.id == typedID)

        // Now retry the typed message; it must reuse the original typedID.
        companion.send()
        await companion.sendTask?.value
        #expect(companion.unsent == nil)
        #expect(transport.requests.count == 3)
        #expect(transport.requests[2].body["message_id"] as? String == typedID)
    }

    @Test
    func failedTypedMessageWithEditedDraftMovesToHeldMessagesAndReusesID() async {
        let transport = StubTransport(statusCode: 500, responseBody: ["detail": "offline"])
        let companion = Companion()
        companion.connectForTesting(transport.makeConnection())
        companion.draft = "first message"
        companion.send()
        // User immediately starts typing a new draft before the failure lands.
        companion.draft = "second draft"
        await companion.sendTask?.value

        #expect(companion.draft == "second draft")
        #expect(companion.heldMessages.count == 1)
        let heldID = companion.heldMessages[0].id
        #expect(transport.requests[0].body["message_id"] as? String == heldID)

        transport.setHandler { req in
            let data = try JSONSerialization.data(withJSONObject: ["ok": true])
            return (
                data,
                HTTPURLResponse(url: req.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            )
        }
        companion.retryHeldMessage()
        await companion.sendTask?.value

        #expect(companion.heldMessages.isEmpty)
        #expect(transport.requests.count == 2)
        #expect(transport.requests[1].body["message_id"] as? String == heldID)
    }

    // MARK: - Helpers

    @Test
    func fixNameReplacesStandaloneBodOnly() {
        #expect(Transcriber.fixName("hey bod") == "hey bud")
        #expect(Transcriber.fixName("Bod, check this") == "bud, check this")
        #expect(Transcriber.fixName("nobody at the bodega") == "nobody at the bodega")
    }

    @Test
    func clipSecondsHandlesZeroRate() {
        #expect(Clip(samples: [0.1, 0.2], rate: 0).seconds == 0)
        #expect(Clip(samples: Array(repeating: 0, count: 8_000), rate: 16_000).seconds == 0.5)
    }
}
