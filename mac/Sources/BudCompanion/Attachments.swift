import AppKit
import Foundation
import ImageIO
import PDFKit
import SwiftUI
import UniformTypeIdentifiers

struct ChatAttachment: Codable, Identifiable, Equatable, Sendable {
    let id: String
    let name: String

    var type: UTType? {
        UTType(filenameExtension: (name as NSString).pathExtension)
    }

    var query: [URLQueryItem] { [URLQueryItem(name: "name", value: name)] }

    func path(in session: String) -> String {
        "sessions/\(session)/attachments/\(id)"
    }

    static func decode(_ value: Any?) -> [ChatAttachment] {
        guard let value, let data = try? JSONSerialization.data(withJSONObject: value)
        else { return [] }
        return (try? JSONDecoder().decode([ChatAttachment].self, from: data)) ?? []
    }

    var fields: [String: String] { ["id": id, "name": name] }
}

struct DraftAttachment: Identifiable, Equatable, Sendable {
    static let limit = 25 * 1024 * 1024
    let reference: ChatAttachment
    let data: Data
    var id: String { reference.id }

    static func read(_ url: URL) throws -> Self {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let values = try url.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey])
        guard values.isRegularFile == true, let size = values.fileSize, size <= limit else {
            throw CocoaError(.fileReadTooLarge)
        }
        let file = try FileHandle(forReadingFrom: url)
        defer { try? file.close() }
        let data = try file.read(upToCount: limit + 1) ?? Data()
        guard data.count <= limit else { throw CocoaError(.fileReadTooLarge) }
        return Self(
            reference: ChatAttachment(
                id: UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased(),
                name: url.lastPathComponent),
            data: data)
    }
}

struct AttachmentChip: View {
    let name: String

    var body: some View {
        Label(name, systemImage: "doc")
            .font(Paper.Typography.callout)
            .lineLimit(1)
            .truncationMode(.middle)
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background(Paper.elevated, in: RoundedRectangle(cornerRadius: Paper.radiusMD))
    }
}

private final class ThumbnailState: ObservableObject {
    @Published var image: NSImage?
}

/// A small image preview in the message. The original opens in the inspector.
struct SentAttachment: View {
    let file: ChatAttachment
    let selection: AttachmentSelection?
    let open: () -> Void
    @StateObject private var thumbnail = ThumbnailState()

    var body: some View {
        Button(action: open) {
            if let image = thumbnail.image {
                Image(nsImage: image)
                    .resizable()
                    .scaledToFit()
                    .frame(maxWidth: 260, maxHeight: 180)
                    .clipShape(RoundedRectangle(cornerRadius: Paper.radiusMD))
            } else {
                AttachmentChip(name: file.name)
            }
        }
        .buttonStyle(.plain)
        .help("Open \(file.name)")
        .accessibilityLabel("Open attachment \(file.name)")
        .task(id: selection?.id) {
            guard file.type?.conforms(to: .image) == true, let selection,
                  let data = try? await selection.connection.getBytes(
                    file.path(in: selection.session), queryItems: file.query),
                  !Task.isCancelled, let source = CGImageSourceCreateWithData(data as CFData, nil),
                  let image = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                    kCGImageSourceCreateThumbnailFromImageAlways: true,
                    kCGImageSourceCreateThumbnailWithTransform: true,
                    kCGImageSourceThumbnailMaxPixelSize: 520,
                  ] as CFDictionary)
            else { return }
            thumbnail.image = NSImage(cgImage: image, size: .zero)
        }
    }
}

struct AttachmentSelection: Identifiable {
    let file: ChatAttachment
    let session: String
    let connection: BudConnection
    var id: String { "\(session)/\(file.id)" }
}

@MainActor
private final class AttachmentInspectorState: ObservableObject {
    @Published var content: AttachmentInspector.Content?
    @Published var expanded = false
    @Published var showDesktops = false
}

@MainActor
struct AttachmentInspector: View {
    let selection: AttachmentSelection
    let close: () -> Void
    @StateObject private var state = AttachmentInspectorState()

    fileprivate enum Content {
        case pdf(PDFDocument)
        case image(NSImage)
        case markdown(String)
        case text(String)
        case error(String)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Text(selection.file.name)
                    .font(Paper.Typography.callout)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(selection.file.name)
                Spacer()
                Button { state.showDesktops.toggle() } label: {
                    Image(systemName: "desktopcomputer.and.arrow.down")
                }
                .buttonStyle(IconButton(size: 24))
                .help("Verify on paired desktops")
                .accessibilityLabel("Verify on paired desktops")
                .popover(isPresented: $state.showDesktops, arrowEdge: .bottom) {
                    DesktopHandoffPicker(selection: selection).padding(16)
                }
                Button { state.expanded.toggle() } label: {
                    Image(systemName: state.expanded
                        ? "arrow.down.right.and.arrow.up.left"
                        : "arrow.up.left.and.arrow.down.right")
                }
                .buttonStyle(IconButton(size: 24))
                .help(state.expanded ? "Narrow preview" : "Expand preview")
                .accessibilityLabel(state.expanded ? "Narrow preview" : "Expand preview")
                Button(action: close) { Image(systemName: "xmark") }
                    .buttonStyle(IconButton(size: 24))
                    .help("Close preview")
                    .accessibilityLabel("Close preview")
            }
            .padding(12)
            Divider()
            switch state.content {
            case .pdf(let document):
                PDFPreview(document: document)
            case .image(let image):
                Image(nsImage: image)
                    .resizable().scaledToFit()
                    .accessibilityLabel(selection.file.name)
                    .padding(12)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .markdown(let markdown):
                ScrollView { MarkdownView(markdown).textSelection(.enabled).padding(16) }
            case .text(let text):
                ScrollView {
                    Text(verbatim: text).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading).padding(16)
                }
            case .error(let error):
                Text(error).foregroundStyle(Paper.inkSecondary).padding(16)
                Spacer()
            case nil:
                ProgressView().controlSize(.small)
                    .frame(maxWidth: .infinity).padding(24)
                Spacer()
            }
        }
        .inspectorColumnWidth(min: state.expanded ? 480 : 240, ideal: state.expanded ? 580 : 320, max: 900)
        .task(id: selection.id) { await load() }
    }

    private func load() async {
        state.content = nil
        let file = selection.file
        let path = file.path(in: selection.session)
        let connection = selection.connection
        do {
            let next: Content
            let ext = (file.name as NSString).pathExtension.lowercased()
            let isMarkdown = ext == "md" || ext == "markdown"
            let isText = isMarkdown || (ext != "csv" && file.type?.conforms(to: .plainText) == true)
            if file.type?.conforms(to: .pdf) == true || file.type?.conforms(to: .image) == true
                || isText
            {
                let data = try await connection.getBytes(path, queryItems: file.query)
                if file.type?.conforms(to: .pdf) == true, let document = PDFDocument(data: data) {
                    next = .pdf(document)
                } else if file.type?.conforms(to: .image) == true, let image = NSImage(data: data) {
                    next = .image(image)
                } else if isText, let text = String(data: data, encoding: .utf8) {
                    next = isMarkdown ? .markdown(text) : .text(text)
                } else {
                    next = .error("Bud couldn’t display this file.")
                }
            } else {
                let parsed = try await connection.send(
                    "GET", path, queryItems: file.query + [URLQueryItem(name: "preview", value: "true")])
                next = (parsed["markdown"] as? String).map(Content.markdown)
                    ?? .error("Bud couldn’t display this file.")
            }
            if !Task.isCancelled { state.content = next }
        } catch {
            if !Task.isCancelled { state.content = .error("\(error)") }
        }
    }
}

private struct PDFPreview: NSViewRepresentable {
    let document: PDFDocument

    func makeNSView(context: Context) -> PDFView {
        let view = PDFView()
        view.autoScales = true
        view.displayMode = .singlePageContinuous
        return view
    }

    func updateNSView(_ view: PDFView, context: Context) {
        if view.document !== document { view.document = document }
    }
}
