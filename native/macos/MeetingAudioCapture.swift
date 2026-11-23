// Meeting Audio Capture
//
// Built by scripts/setup_macos.sh with the Xcode command-line tools:
//   swiftc -O -framework AVFoundation -framework CoreAudio -framework AudioToolbox
//          -framework CoreMedia -framework ScreenCaptureKit -framework AppKit
//          -framework CoreGraphics -o meeting-audio-capture MeetingAudioCapture.swift
//
// Subcommands:
//   probe         JSON windows + processes whose microphone is running
//   wait-session  exit after the Mac wakes or the screen unlocks
//   record        mix system audio and the microphone into one 16 kHz WAV
//
// System audio uses a Core Audio process tap (macOS 14.2+, the API the
// 14.4 system-audio permission is for). If that tap cannot be created,
// ScreenCaptureKit captures system audio instead. Neither path installs a
// virtual audio device and neither joins the call. The original ffmpeg
// recorder remains the fallback when this program is not installed.
//
// Grant, when macOS asks:
//   Microphone
//   Screen & System Audio Recording
// The first prompt is for this app (Meeting Audio Capture). A later prompt
// may name the terminal that launched it.

import AppKit
import AudioToolbox
import AVFoundation
import CoreAudio
import CoreGraphics
import CoreMedia
import Foundation
import ScreenCaptureKit

private func fourCC(_ text: String) -> UInt32 {
    var value: UInt32 = 0
    for byte in text.utf8.prefix(4) {
        value = (value << 8) + UInt32(byte)
    }
    return value
}

private func logLine(_ text: String) {
    fputs(text + "\n", stderr)
    fflush(stderr)
}

// MARK: - System audio permission
//
// A Core Audio process tap is created even when "System Audio Recording" was
// never granted to this app. macOS then feeds it digital silence, so the
// others track is all zeros and every line is labeled as the microphone
// owner. TCC is a private framework; the check is best effort and returns
// "unknown" when it cannot be loaded.

private typealias TCCPreflightFunction = @convention(c) (CFString, CFDictionary?) -> Int
private typealias TCCRequestFunction = @convention(c) (CFString, CFDictionary?, @escaping (Bool) -> Void) -> Void

private let tccHandle: UnsafeMutableRawPointer? = dlopen(
    "/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC",
    RTLD_NOW
)

/// "granted", "denied", or "unknown" (never asked, or the check is unavailable).
private func systemAudioPermission() -> String {
    guard let handle = tccHandle, let symbol = dlsym(handle, "TCCAccessPreflight") else {
        return "unknown"
    }
    let preflight = unsafeBitCast(symbol, to: TCCPreflightFunction.self)
    switch preflight("kTCCServiceAudioCapture" as CFString, nil) {
    case 0:
        return "granted"
    case 1:
        return "denied"
    default:
        return "unknown"
    }
}

private func requestSystemAudioPermission() -> Bool {
    guard let handle = tccHandle, let symbol = dlsym(handle, "TCCAccessRequest") else {
        return false
    }
    let request = unsafeBitCast(symbol, to: TCCRequestFunction.self)
    let semaphore = DispatchSemaphore(value: 0)
    let result = StopFlag()
    request("kTCCServiceAudioCapture" as CFString, nil) { granted in
        if granted {
            result.stop()
        }
        semaphore.signal()
    }
    _ = semaphore.wait(timeout: .now() + 120)
    return result.isSet()
}

// MARK: - Stop flag

final class StopFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var stopped = false

    func stop() {
        lock.lock()
        stopped = true
        lock.unlock()
    }

    func isSet() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return stopped
    }
}

// MARK: - WAV

final class WavWriter {
    private let handle: FileHandle
    private var frames: UInt32 = 0
    private let sampleRate: UInt32 = 16000

    init(path: String) throws {
        let url = URL(fileURLWithPath: path)
        FileManager.default.createFile(atPath: path, contents: nil)
        handle = try FileHandle(forWritingTo: url)
        try handle.write(contentsOf: Self.header(dataBytes: 0, sampleRate: sampleRate))
    }

    func append(_ samples: [Int16]) {
        if samples.isEmpty {
            return
        }
        var data = Data(capacity: samples.count * 2)
        for sample in samples {
            var little = sample.littleEndian
            withUnsafeBytes(of: &little) { data.append(contentsOf: $0) }
        }
        try? handle.write(contentsOf: data)
        frames += UInt32(samples.count)
    }

    func close() {
        let bytes = frames * 2
        try? handle.seek(toOffset: 0)
        try? handle.write(contentsOf: Self.header(dataBytes: bytes, sampleRate: sampleRate))
        try? handle.close()
    }

    private static func header(dataBytes: UInt32, sampleRate: UInt32) -> Data {
        var data = Data()
        func ascii(_ text: String) { data.append(contentsOf: text.utf8) }
        func u32(_ value: UInt32) {
            var little = value.littleEndian
            withUnsafeBytes(of: &little) { data.append(contentsOf: $0) }
        }
        func u16(_ value: UInt16) {
            var little = value.littleEndian
            withUnsafeBytes(of: &little) { data.append(contentsOf: $0) }
        }
        ascii("RIFF")
        u32(36 + dataBytes)
        ascii("WAVE")
        ascii("fmt ")
        u32(16)
        u16(1)
        u16(1)
        u32(sampleRate)
        u32(sampleRate * 2)
        u16(2)
        u16(16)
        ascii("data")
        u32(dataBytes)
        return data
    }
}

// MARK: - Resampler

final class Resampler {
    private let step: Double
    private var position: Double = 0
    private var previous: Float = 0

    init(from sourceRate: Double, to targetRate: Double) {
        let source = sourceRate > 0 ? sourceRate : targetRate
        step = source / targetRate
    }

    func process(_ input: [Float]) -> [Float] {
        if input.isEmpty {
            return []
        }
        let count = input.count
        let last = Double(count - 1)
        if position > last {
            position -= Double(count)
            previous = input[count - 1]
            return []
        }
        var output: [Float] = []
        while position <= last {
            let index = Int(floor(position))
            let fraction = Float(position - Double(index))
            let left = index < 0 ? previous : input[index]
            let rightIndex = index + 1
            let right = rightIndex < count ? input[rightIndex] : input[count - 1]
            output.append(left + (right - left) * fraction)
            position += step
        }
        position -= Double(count)
        previous = input[count - 1]
        return output
    }
}

// MARK: - Mix

final class MixBuffer: @unchecked Sendable {
    private let lock = NSLock()
    private var microphone: [Float] = []
    private var system: [Float] = []
    private let cap = 16000 * 8

    func pushMicrophone(_ samples: [Float]) {
        push(&microphone, samples)
    }

    func pushSystem(_ samples: [Float]) {
        push(&system, samples)
    }

    private func push(_ buffer: inout [Float], _ samples: [Float]) {
        if samples.isEmpty {
            return
        }
        lock.lock()
        buffer.append(contentsOf: samples)
        if buffer.count > cap {
            buffer.removeFirst(buffer.count - cap)
        }
        lock.unlock()
    }

    func take(frames: Int) -> (mic: [Float], system: [Float]) {
        lock.lock()
        defer { lock.unlock() }
        return (take(&microphone, frames), take(&system, frames))
    }

    private func take(_ buffer: inout [Float], _ frames: Int) -> [Float] {
        var output = [Float](repeating: 0, count: frames)
        let have = min(frames, buffer.count)
        if have > 0 {
            for index in 0..<have {
                output[index] = buffer[index]
            }
            buffer.removeFirst(have)
        }
        return output
    }
}

private func monoFloats(pointer: UnsafePointer<Float>, count: Int, channels: Int) -> [Float] {
    if count <= 0 {
        return []
    }
    if channels <= 1 {
        return Array(UnsafeBufferPointer(start: pointer, count: count))
    }
    let frames = count / channels
    var mono = [Float]()
    mono.reserveCapacity(frames)
    for frame in 0..<frames {
        var sum: Float = 0
        for channel in 0..<channels {
            sum += pointer[frame * channels + channel]
        }
        mono.append(sum / Float(channels))
    }
    return mono
}

private func floatsFromBufferList(
    _ bufferList: UnsafePointer<AudioBufferList>,
    format: AudioStreamBasicDescription
) -> [Float] {
    let flags = format.mFormatFlags
    if (flags & kAudioFormatFlagIsFloat) == 0 {
        return []
    }
    let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: bufferList))
    let channels = max(1, Int(format.mChannelsPerFrame))
    let interleaved = (flags & kAudioFormatFlagIsNonInterleaved) == 0
    if interleaved {
        guard let first = buffers.first, let data = first.mData else {
            return []
        }
        let count = Int(first.mDataByteSize) / MemoryLayout<Float>.size
        let pointer = data.bindMemory(to: Float.self, capacity: max(count, 1))
        return monoFloats(pointer: pointer, count: count, channels: channels)
    }
    var frames = 0
    for buffer in buffers {
        let count = Int(buffer.mDataByteSize) / MemoryLayout<Float>.size
        frames = max(frames, count)
    }
    if frames == 0 {
        return []
    }
    var mono = [Float](repeating: 0, count: frames)
    var used = 0
    for buffer in buffers {
        guard let data = buffer.mData else {
            continue
        }
        let count = Int(buffer.mDataByteSize) / MemoryLayout<Float>.size
        let pointer = data.bindMemory(to: Float.self, capacity: max(count, 1))
        for index in 0..<min(count, frames) {
            mono[index] += pointer[index]
        }
        used += 1
    }
    if used > 1 {
        let scale = Float(used)
        for index in 0..<frames {
            mono[index] /= scale
        }
    }
    return mono
}

// MARK: - System audio

final class SystemCapture: @unchecked Sendable {
    let mix: MixBuffer
    let resamplerLock = NSLock()
    var resampler = Resampler(from: 48000, to: 16000)
    var format = AudioStreamBasicDescription()
    var tapID = AudioObjectID(kAudioObjectUnknown)
    var aggregateID = AudioObjectID(kAudioObjectUnknown)
    var procID: AudioDeviceIOProcID?
    var stream: SCStream?
    var keepAlive: AnyObject?
    let backend: String
    // Set once any non-zero sample arrives. A tap without the System Audio
    // Recording grant only ever delivers exact zeros.
    let signalSeen = StopFlag()

    private func noteSignal(_ samples: [Float]) {
        if signalSeen.isSet() {
            return
        }
        if samples.contains(where: { $0 != 0 }) {
            signalSeen.stop()
        }
    }

    init(mix: MixBuffer, backend: String) {
        self.mix = mix
        self.backend = backend
    }

    func consume(bufferList: UnsafePointer<AudioBufferList>) {
        let samples = floatsFromBufferList(bufferList, format: format)
        noteSignal(samples)
        resamplerLock.lock()
        let converted = resampler.process(samples)
        resamplerLock.unlock()
        mix.pushSystem(converted)
    }

    func consume(sampleBuffer: CMSampleBuffer) {
        guard let description = CMSampleBufferGetFormatDescription(sampleBuffer),
              let basic = CMAudioFormatDescriptionGetStreamBasicDescription(description) else {
            return
        }
        let asbd = basic.pointee
        var listSize = 0
        var block: CMBlockBuffer?
        var audio = AudioBufferList(
            mNumberBuffers: 1,
            mBuffers: AudioBuffer(mNumberChannels: 0, mDataByteSize: 0, mData: nil)
        )
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: &listSize,
            bufferListOut: &audio,
            bufferListSize: MemoryLayout<AudioBufferList>.size,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
            blockBufferOut: &block
        )
        if status != noErr || audio.mBuffers.mData == nil {
            return
        }
        let count = Int(audio.mBuffers.mDataByteSize) / MemoryLayout<Float>.size
        let pointer = audio.mBuffers.mData!.bindMemory(to: Float.self, capacity: max(count, 1))
        let channels = max(1, Int(audio.mBuffers.mNumberChannels))
        let mono = monoFloats(pointer: pointer, count: count, channels: channels)
        noteSignal(mono)
        resamplerLock.lock()
        if asbd.mSampleRate > 0 && abs(asbd.mSampleRate - sourceRate) > 1 {
            sourceRate = asbd.mSampleRate
            resampler = Resampler(from: sourceRate, to: 16000)
        }
        let converted = resampler.process(mono)
        resamplerLock.unlock()
        mix.pushSystem(converted)
    }

    private var sourceRate: Double = 48000
}

private let ioProc: AudioDeviceIOProc = { _, _, inputData, _, _, _, client in
    guard let client = client else {
        return noErr
    }
    let capture = Unmanaged<SystemCapture>.fromOpaque(client).takeUnretainedValue()
    capture.consume(bufferList: inputData)
    return noErr
}

private func readObjectString(_ objectID: AudioObjectID, selector: UInt32) -> String? {
    var address = AudioObjectPropertyAddress(
        mSelector: AudioObjectPropertySelector(selector),
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var size: UInt32 = 0
    if AudioObjectGetPropertyDataSize(objectID, &address, 0, nil, &size) != noErr || size == 0 {
        return nil
    }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<CFTypeRef>.alignment)
    defer { raw.deallocate() }
    if AudioObjectGetPropertyData(objectID, &address, 0, nil, &size, raw) != noErr {
        return nil
    }
    let value = raw.load(as: CFString.self)
    return value as String
}

private func streamFormat(_ deviceID: AudioObjectID) -> AudioStreamBasicDescription? {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreamFormat,
        mScope: kAudioObjectPropertyScopeInput,
        mElement: kAudioObjectPropertyElementMain
    )
    var format = AudioStreamBasicDescription()
    var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    if AudioObjectGetPropertyData(deviceID, &address, 0, nil, &size, &format) != noErr {
        return nil
    }
    return format
}

private func defaultOutputDeviceUID() -> String? {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultSystemOutputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var deviceID = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let system = AudioObjectID(kAudioObjectSystemObject)
    if AudioObjectGetPropertyData(system, &address, 0, nil, &size, &deviceID) != noErr {
        return nil
    }
    if deviceID == kAudioObjectUnknown {
        return nil
    }
    return readObjectString(deviceID, selector: kAudioDevicePropertyDeviceUID)
}

@available(macOS 14.2, *)
private func startProcessTap(mix: MixBuffer) -> SystemCapture? {
    // Process-object IDs, not pids. An empty list taps every process. This
    // helper plays no audio, so excluding it is unnecessary.
    let description = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
    description.name = "Meeting Notes System Audio"
    description.isPrivate = true
    description.isExclusive = false
    description.muteBehavior = CATapMuteBehavior.unmuted
    if description.uuid.uuidString.isEmpty {
        description.uuid = UUID()
    }
    var tapID = AudioObjectID(kAudioObjectUnknown)
    let created = AudioHardwareCreateProcessTap(description, &tapID)
    if created != noErr {
        logLine("capture: process tap was not created (status \(created))")
        return nil
    }
    // The property read failed on macOS 26 ("process tap has no uid") even
    // after the tap was created. The description UUID is the tap UID.
    var uid = ""
    if let property = readObjectString(tapID, selector: kAudioTapPropertyUID), !property.isEmpty {
        uid = property
    } else {
        uid = description.uuid.uuidString
    }
    if uid.isEmpty {
        AudioHardwareDestroyProcessTap(tapID)
        logLine("capture: process tap has no uid")
        return nil
    }
    guard let outputUID = defaultOutputDeviceUID() else {
        AudioHardwareDestroyProcessTap(tapID)
        logLine("capture: no default output device; process tap was not started")
        return nil
    }
    let aggregateUID = UUID().uuidString
    // The tap is not a sub-device. It goes in TapList. The aggregate's clock
    // is the default output device.
    let aggregate: [String: Any] = [
        kAudioAggregateDeviceNameKey as String: "Meeting Notes Aggregate",
        kAudioAggregateDeviceUIDKey as String: aggregateUID,
        kAudioAggregateDeviceIsPrivateKey as String: true,
        kAudioAggregateDeviceIsStackedKey as String: false,
        kAudioAggregateDeviceTapAutoStartKey as String: true,
        kAudioAggregateDeviceMainSubDeviceKey as String: outputUID,
        kAudioAggregateDeviceSubDeviceListKey as String: [
            [kAudioSubDeviceUIDKey as String: outputUID],
        ],
        kAudioAggregateDeviceTapListKey as String: [
            [
                kAudioSubTapDriftCompensationKey as String: true,
                kAudioSubTapUIDKey as String: uid,
            ],
        ],
    ]
    var aggregateID = AudioObjectID(kAudioObjectUnknown)
    let aggregated = AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &aggregateID)
    if aggregated != noErr {
        AudioHardwareDestroyProcessTap(tapID)
        logLine("capture: aggregate device was not created (status \(aggregated))")
        return nil
    }
    let capture = SystemCapture(mix: mix, backend: "coreaudio-tap")
    capture.tapID = tapID
    capture.aggregateID = aggregateID
    if let format = streamFormat(aggregateID) {
        capture.format = format
        capture.resampler = Resampler(from: format.mSampleRate, to: 16000)
    }
    var proc: AudioDeviceIOProcID?
    let procStatus = AudioDeviceCreateIOProcID(
        aggregateID,
        ioProc,
        Unmanaged.passUnretained(capture).toOpaque(),
        &proc
    )
    if procStatus != noErr || proc == nil {
        AudioHardwareDestroyAggregateDevice(aggregateID)
        AudioHardwareDestroyProcessTap(tapID)
        logLine("capture: could not listen to the process tap (status \(procStatus))")
        return nil
    }
    capture.procID = proc
    let started = AudioDeviceStart(aggregateID, proc)
    if started != noErr {
        AudioDeviceDestroyIOProcID(aggregateID, proc!)
        AudioHardwareDestroyAggregateDevice(aggregateID)
        AudioHardwareDestroyProcessTap(tapID)
        logLine("capture: process tap did not start (status \(started))")
        return nil
    }
    logLine("capture-backend: coreaudio-tap")
    return capture
}

private final class ScreenAudioOutput: NSObject, SCStreamOutput {
    let capture: SystemCapture

    init(capture: SystemCapture) {
        self.capture = capture
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        if type == .audio {
            capture.consume(sampleBuffer: sampleBuffer)
        }
    }
}

private func startScreenCapture(mix: MixBuffer) -> SystemCapture? {
    if #available(macOS 13.0, *) {
        let semaphore = DispatchSemaphore(value: 0)
        let capture = SystemCapture(mix: mix, backend: "screencapturekit")
        let output = ScreenAudioOutput(capture: capture)
        let started = StopFlag()
        Task.detached {
            defer { semaphore.signal() }
            do {
                let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
                guard let display = content.displays.first else {
                    logLine("capture: ScreenCaptureKit found no display")
                    return
                }
                let filter = SCContentFilter(
                    display: display,
                    excludingApplications: [SCRunningApplication](),
                    exceptingWindows: [SCWindow]()
                )
                let config = SCStreamConfiguration()
                config.capturesAudio = true
                config.excludesCurrentProcessAudio = true
                config.sampleRate = 48000
                config.channelCount = 2
                config.width = 2
                config.height = 2
                config.showsCursor = false
                let stream = SCStream(filter: filter, configuration: config, delegate: nil)
                try stream.addStreamOutput(output, type: .audio, sampleHandlerQueue: DispatchQueue(label: "meeting-notes.sck"))
                try await stream.startCapture()
                capture.stream = stream
                // addStreamOutput retains `output`. Hold it here too so the
                // object cannot disappear before the stream has it.
                capture.keepAlive = output
                started.stop()
            } catch {
                logLine("capture: ScreenCaptureKit failed (\(error))")
            }
        }
        semaphore.wait()
        if started.isSet() {
            logLine("capture-backend: screencapturekit")
            return capture
        }
    }
    return nil
}

private func stopSystem(_ capture: SystemCapture?) {
    guard let capture = capture else {
        return
    }
    if let proc = capture.procID, capture.aggregateID != kAudioObjectUnknown {
        AudioDeviceStop(capture.aggregateID, proc)
        AudioDeviceDestroyIOProcID(capture.aggregateID, proc)
    }
    if capture.aggregateID != kAudioObjectUnknown {
        AudioHardwareDestroyAggregateDevice(capture.aggregateID)
    }
    if capture.tapID != kAudioObjectUnknown {
        AudioHardwareDestroyProcessTap(capture.tapID)
    }
    if let stream = capture.stream {
        let semaphore = DispatchSemaphore(value: 0)
        Task.detached {
            try? await stream.stopCapture()
            semaphore.signal()
        }
        _ = semaphore.wait(timeout: .now() + 2)
    }
}

// MARK: - Microphone

private func startMicrophone(mix: MixBuffer) -> AVAudioEngine? {
    let engine = AVAudioEngine()
    let input = engine.inputNode
    let format = input.outputFormat(forBus: 0)
    if format.sampleRate <= 0 || format.channelCount == 0 {
        logLine("capture: microphone format is empty. Grant Microphone access to Meeting Audio Capture.")
        return nil
    }
    let resampler = Resampler(from: format.sampleRate, to: 16000)
    let channels = Int(format.channelCount)
    input.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in
        guard let channel = buffer.floatChannelData else {
            return
        }
        let frames = Int(buffer.frameLength)
        var mono = [Float](repeating: 0, count: frames)
        let used = max(1, channels)
        for frame in 0..<frames {
            var sum: Float = 0
            for index in 0..<used {
                sum += channel[index][frame]
            }
            mono[frame] = sum / Float(used)
        }
        mix.pushMicrophone(resampler.process(mono))
    }
    do {
        try engine.start()
    } catch {
        logLine("capture: microphone did not start (\(error)). Grant Microphone access.")
        return nil
    }
    logLine("capture: microphone open (\(Int(format.sampleRate)) Hz)")
    return engine
}

// MARK: - Record

private func pcm(_ samples: [Float], gain: Float = 1) -> [Int16] {
    var mixed = [Int16]()
    mixed.reserveCapacity(samples.count)
    for sample in samples {
        let clipped = max(-1 as Float, min(1, sample * gain))
        mixed.append(Int16(clipped * 32767))
    }
    return mixed
}

private func record(outputPath: String, selfPath: String?, othersPath: String?) {
    let flag = StopFlag()
    signal(SIGINT, SIG_IGN)
    signal(SIGTERM, SIG_IGN)
    let interrupt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .global())
    interrupt.setEventHandler { flag.stop() }
    interrupt.resume()
    let terminate = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global())
    terminate.setEventHandler { flag.stop() }
    terminate.resume()
    DispatchQueue.global().async {
        while let line = readLine(strippingNewline: false) {
            if line.contains("q") {
                flag.stop()
                return
            }
        }
        flag.stop()
    }

    let mix = MixBuffer()
    var system: SystemCapture?
    // MEETING_NOTES_SYSTEM_AUDIO: auto (default), tap, or screencapturekit.
    // auto uses the process tap only when System Audio Recording is granted;
    // otherwise ScreenCaptureKit first and the tap as the last resort.
    let preference = (ProcessInfo.processInfo.environment["MEETING_NOTES_SYSTEM_AUDIO"] ?? "auto")
        .trimmingCharacters(in: .whitespaces).lowercased()
    let permission = systemAudioPermission()
    logLine("capture: system audio permission \(permission)")
    let preferScreen = preference == "screencapturekit" || preference == "sck"
        || (preference != "tap" && permission != "granted")
    if preferScreen {
        logLine("capture: using ScreenCaptureKit for system audio (process tap needs System Audio Recording)")
        system = startScreenCapture(mix: mix)
    }
    if system == nil {
        if #available(macOS 14.2, *) {
            system = startProcessTap(mix: mix)
        } else {
            logLine("capture: process taps need macOS 14.2 or newer")
        }
    }
    if system == nil && !preferScreen {
        logLine("capture: trying ScreenCaptureKit for system audio")
        system = startScreenCapture(mix: mix)
    }
    if system == nil {
        logLine(
            "capture-error: system audio is unavailable. Core Audio process tap and " +
            "ScreenCaptureKit both failed. AVFoundation cannot record other apps, so " +
            "BlackHole is only useful if you set MEETING_NOTES_CAPTURE=avfoundation and " +
            "point MEETING_NOTES_AVFOUNDATION_DEVICE at a virtual device you installed. " +
            "Grant Screen & System Audio Recording to Meeting Audio Capture and run again " +
            "to capture the call without BlackHole."
        )
    }
    let engine = startMicrophone(mix: mix)
    if engine == nil && system == nil {
        logLine("capture-error: neither the microphone nor system audio could be opened")
        exit(2)
    }

    let writer: WavWriter
    var selfWriter: WavWriter?
    var othersWriter: WavWriter?
    do {
        writer = try WavWriter(path: outputPath)
        if let selfPath = selfPath, let othersPath = othersPath {
            selfWriter = try WavWriter(path: selfPath)
            othersWriter = try WavWriter(path: othersPath)
        }
    } catch {
        logLine("capture-error: could not open \(outputPath) (\(error))")
        exit(1)
    }
    logLine("capture: writing \(outputPath)")
    if selfWriter != nil {
        logLine("capture: writing separate tracks audio-self.wav and audio-others.wav")
    }
    let frameCount = 1600
    let silenceLimit = 150  // 15 s of 0.1 s steps
    var steps = 0
    var silenceChecked = false
    while !flag.isSet() {
        Thread.sleep(forTimeInterval: 0.1)
        steps += 1
        if !silenceChecked && steps >= silenceLimit {
            silenceChecked = true
            if let current = system, current.backend == "coreaudio-tap", !current.signalSeen.isSet() {
                logLine("capture: process tap delivered only digital silence for 15 s; switching to ScreenCaptureKit")
                stopSystem(current)
                system = nil
                if let screen = startScreenCapture(mix: mix) {
                    system = screen
                } else if #available(macOS 14.2, *) {
                    logLine("capture: ScreenCaptureKit failed; keeping the process tap")
                    system = startProcessTap(mix: mix)
                }
            }
        }
        let taken = mix.take(frames: frameCount)
        var mixedSamples = [Float]()
        mixedSamples.reserveCapacity(frameCount)
        for index in 0..<frameCount {
            mixedSamples.append(taken.mic[index] * 0.7 + taken.system[index])
        }
        writer.append(pcm(mixedSamples))
        selfWriter?.append(pcm(taken.mic))
        othersWriter?.append(pcm(taken.system))
    }
    writer.close()
    selfWriter?.close()
    othersWriter?.close()
    engine?.stop()
    stopSystem(system)
    logLine("capture: stopped")
}

// MARK: - Probe

private func readUInt32(_ objectID: AudioObjectID, selector: AudioObjectPropertySelector) -> UInt32? {
    var address = AudioObjectPropertyAddress(
        mSelector: AudioObjectPropertySelector(selector),
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var value: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    if AudioObjectGetPropertyData(objectID, &address, 0, nil, &size, &value) != noErr {
        return nil
    }
    return value
}

private func readPID(_ objectID: AudioObjectID) -> pid_t? {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioProcessPropertyPID,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var value: pid_t = 0
    var size = UInt32(MemoryLayout<pid_t>.size)
    if AudioObjectGetPropertyData(objectID, &address, 0, nil, &size, &value) != noErr {
        return nil
    }
    return value
}

@_silgen_name("proc_name")
private func cProcName(_ pid: pid_t, _ buffer: UnsafeMutablePointer<CChar>?, _ buffersize: UInt32) -> Int32

private func processName(_ pid: pid_t) -> String {
    if let app = NSRunningApplication(processIdentifier: pid) {
        if let name = app.localizedName, !name.isEmpty {
            return name
        }
    }
    var buffer = [CChar](repeating: 0, count: 256)
    let length = cProcName(pid, &buffer, UInt32(buffer.count))
    if length > 0 {
        return String(cString: buffer)
    }
    return ""
}

private func audioInputs() -> (items: [[String: Any]], available: Bool) {
    // macOS 14.2+: process objects, then whether each one is recording.
    // An empty list is a successful answer (nobody is using the microphone),
    // not a failed probe. A failed property read is the unavailable case.
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyProcessObjectList,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var size: UInt32 = 0
    let system = AudioObjectID(kAudioObjectSystemObject)
    if AudioObjectGetPropertyDataSize(system, &address, 0, nil, &size) != noErr {
        return ([], false)
    }
    if size == 0 {
        return ([], true)
    }
    let count = Int(size) / MemoryLayout<AudioObjectID>.size
    var identifiers = [AudioObjectID](repeating: 0, count: max(count, 1))
    if AudioObjectGetPropertyData(system, &address, 0, nil, &size, &identifiers) != noErr {
        return ([], false)
    }
    var items: [[String: Any]] = []
    var sawInputProperty = false
    for identifier in identifiers {
        let running = readUInt32(identifier, selector: kAudioProcessPropertyIsRunningInput)
        if running == nil {
            continue
        }
        sawInputProperty = true
        guard running != 0, let pid = readPID(identifier), pid > 0 else {
            continue
        }
        let app = NSRunningApplication(processIdentifier: pid)
        let bundle = readObjectString(identifier, selector: kAudioProcessPropertyBundleID)
            ?? app?.bundleIdentifier
            ?? ""
        items.append([
            "name": processName(pid),
            "bundle_id": bundle,
            "pid": Int(pid),
            "input_running": true,
        ])
    }
    if !sawInputProperty {
        return ([], false)
    }
    return (items, true)
}

private func reportPermissions(request: Bool) {
    // Called in this process, after responsibility was disclaimed, so the
    // prompt and the Privacy list name Meeting Audio Capture.
    var granted = CGPreflightScreenCaptureAccess()
    if request && !granted {
        granted = CGRequestScreenCaptureAccess()
    }
    var audio = systemAudioPermission()
    if request && audio != "granted" {
        audio = requestSystemAudioPermission() ? "granted" : systemAudioPermission()
    }
    let payload: [String: Any] = [
        "screen_recording": granted,
        "system_audio": audio,
        "app": "Meeting Audio Capture",
    ]
    if let data = try? JSONSerialization.data(withJSONObject: payload) {
        FileHandle.standardOutput.write(data)
    } else {
        fputs("{\"screen_recording\":false}\n", stdout)
        return
    }
    fputs("\n", stdout)
}

private func probe() {
    let options = CGWindowListOption(arrayLiteral: .optionOnScreenOnly, .excludeDesktopElements)
    let raw = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] ?? []
    var windows: [[String: Any]] = []
    for item in raw {
        let layer = item[kCGWindowLayer as String] as? Int ?? 0
        if layer != 0 {
            continue
        }
        let owner = item[kCGWindowOwnerName as String] as? String ?? ""
        let title = item[kCGWindowName as String] as? String ?? ""
        let pid = item[kCGWindowOwnerPID as String] as? Int ?? 0
        if owner.isEmpty && title.isEmpty {
            continue
        }
        windows.append(["title": title, "owner": owner, "pid": pid])
    }
    let inputs = audioInputs()
    let payload: [String: Any] = [
        "windows": windows,
        "inputs": inputs.items,
        "input_available": inputs.available,
    ]
    guard let data = try? JSONSerialization.data(withJSONObject: payload) else {
        fputs("{}\n", stdout)
        return
    }
    FileHandle.standardOutput.write(data)
    fputs("\n", stdout)
}

// MARK: - Wake / unlock

private func waitSession() -> Never {
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    let center = NSWorkspace.shared.notificationCenter
    center.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { _ in
        fputs("wake\n", stdout)
        fflush(stdout)
        exit(0)
    }
    center.addObserver(forName: NSWorkspace.screensDidWakeNotification, object: nil, queue: .main) { _ in
        fputs("wake\n", stdout)
        fflush(stdout)
        exit(0)
    }
    DistributedNotificationCenter.default().addObserver(
        forName: Notification.Name("com.apple.screenIsUnlocked"),
        object: nil,
        queue: .main
    ) { _ in
        fputs("unlock\n", stdout)
        fflush(stdout)
        exit(0)
    }
    app.run()
    exit(0)
}

// MARK: - Entry

private func argumentValue(_ name: String, in args: [String]) -> String? {
    guard let index = args.firstIndex(of: name), index + 1 < args.count else {
        return nil
    }
    return args[index + 1]
}

let arguments = Array(CommandLine.arguments.dropFirst())
let command = arguments.first ?? ""
switch command {
case "probe":
    probe()
case "permissions":
    reportPermissions(request: arguments.contains("--request"))
case "wait-session":
    waitSession()
case "record":
    guard let output = argumentValue("--output", in: arguments), !output.isEmpty else {
        logLine("usage: meeting-audio-capture record --output PATH [--self PATH --others PATH]")
        exit(2)
    }
    record(
        outputPath: output,
        selfPath: argumentValue("--self", in: arguments),
        othersPath: argumentValue("--others", in: arguments)
    )
default:
    logLine("usage: meeting-audio-capture probe | wait-session | record --output PATH")
    exit(2)
}
